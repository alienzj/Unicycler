"""
This module has functions for finding graph paths connecting two nodes given a consensus read
sequence.

Author: Ryan Wick
email: rrwick@gmail.com
"""

from collections import defaultdict
from .misc import weighted_average, reverse_complement, get_num_agreement
from .cpp_function_wrappers import fully_global_alignment, path_alignment
from . import settings


class TooManyPaths(Exception):
    pass


def get_best_paths_for_seq(graph, start_seg, end_seg, target_length, sequence, scoring_scheme,
                           expected_scaled_score):
    """
    Given a sequence and target length, this function finds the best paths from the start
    segment to the end segment.
    """
    # Limit the path search to lengths near the target.
    min_length = int(round(target_length * settings.MIN_RELATIVE_PATH_LENGTH))
    max_length = int(round(target_length * settings.MAX_RELATIVE_PATH_LENGTH))

    # The overlap isn't present in the consensus sequence, so we need to add it on.
    if sequence:
        sequence = graph.seq_from_signed_seg_num(start_seg)[-graph.overlap:] + sequence + \
                   graph.seq_from_signed_seg_num(end_seg)[:graph.overlap]

    # If there are few enough possible paths, we just try aligning to them all.
    try:
        paths = all_paths(graph, start_seg, end_seg, min_length, target_length, max_length)
        progressive_path_search = False

    # If there are too many paths to try exhaustively, we use a progressive approach to find
    # the best path.
    except TooManyPaths:
        progressive_path_search = True
        paths = progressive_path_find(graph, start_seg, end_seg, min_length, max_length,
                                      sequence, scoring_scheme, expected_scaled_score)

    # Sort by length discrepancy from the target so the closest length matches come first.
    paths = sorted(paths, key=lambda x: abs(target_length - graph.get_path_length(x)))

    # We now align the consensus to each of the possible paths.
    paths_and_scores = []
    for path in paths:
        path_len = graph.get_path_length(path)
        length_discrepancy = abs(path_len - target_length)

        # If there is a consensus sequence, then we actually do an alignment against the path.
        if sequence:
            path_seq = graph.get_path_sequence(path)
            alignment_result = fully_global_alignment(sequence, path_seq, scoring_scheme,
                                                      True, 1000)
            if not alignment_result:
                continue

            seqan_parts = alignment_result.split(',', 9)
            raw_score = int(seqan_parts[6])
            scaled_score = float(seqan_parts[7])

        # If there isn't a consensus sequence (i.e. the start and end overlap), then each
        # path is only scored on how well its length agrees with the target length.
        else:
            raw_score = get_num_agreement(path_len, target_length) * 100.0
            scaled_score = 100.0

        paths_and_scores.append((path, raw_score, length_discrepancy, scaled_score))

    # Sort the paths from highest to lowest quality.
    paths_and_scores = sorted(paths_and_scores, key=lambda x: (-x[1], x[2], -x[3]))

    # Don't bother keeping paths which are much worse than the best.
    if paths_and_scores:
        best_scaled_score = paths_and_scores[0][3]
        min_scaled_score = best_scaled_score * 0.95
        paths_and_scores = [x for x in paths_and_scores if x[3] >= min_scaled_score]

    return paths_and_scores, progressive_path_search


def all_paths(graph, start, end, min_length, target_length, max_length):
    """
    Returns a list of all paths which connect the starting segment to the ending segment and
    are within the length bounds. The start and end segments are not themselves included in the
    paths. Returns an empty list if no paths exist.
    Loops in the graph (especially loops of short segments which don't add much to the path
    length) can result in very large numbers of potential paths in complex areas. To somewhat
    manage this, we exclude paths which include too many copies of a segment. 'Too many copies'
    is defined as double the copy depth count or the double the depth over start/end depth.
    """
    if start not in graph.forward_links:
        return []

    start_seg = graph.segments[abs(start)]
    end_seg = graph.segments[abs(end)]
    start_end_depth = weighted_average(start_seg.depth, end_seg.depth,
                                       start_seg.get_length_no_overlap(graph.overlap),
                                       end_seg.get_length_no_overlap(graph.overlap))
    working_paths = [[x] for x in graph.forward_links[start]]
    final_paths = []
    while working_paths:
        new_working_paths = []
        for working_path in working_paths:
            last_seg = working_path[-1]
            if last_seg == end:
                potential_result = working_path[:-1]
                if graph.get_path_length(potential_result) >= min_length:
                    final_paths.append(potential_result)
                    if len(final_paths) > settings.ALL_PATH_SEARCH_MAX_FINAL_PATHS:
                        raise TooManyPaths
            elif graph.get_path_length(working_path) <= max_length and \
                    last_seg in graph.forward_links:
                for next_seg in graph.forward_links[last_seg]:
                    max_allowed_count = graph.max_path_segment_count(next_seg, start_end_depth)
                    count_so_far = working_path.count(next_seg) + working_path.count(-next_seg)
                    if count_so_far < max_allowed_count:
                        new_working_paths.append(working_path + [next_seg])

        # If the number of working paths is too high, we give up.
        if len(working_paths) > settings.ALL_PATH_SEARCH_MAX_WORKING_PATHS:
            raise TooManyPaths
        working_paths = new_working_paths

    return final_paths


def progressive_path_find(graph, start, end, min_length, max_length, sequence, scoring_scheme,
                          expected_scaled_score):
    """
    This function is called when all_paths fails due to too many paths. It searches for paths by
    extended outward from both the start and end, making paths where the two searches meet. When
    the number of working paths gets too high, it is culled by performing alignments with the
    in-progress paths.
    """
    reverse_sequence = reverse_complement(sequence)

    # This set will collect all of the final paths produced. Paths are stored here as tuples
    # (because they are hashable for a set).
    final_paths = set()

    # We will work with a list of forward paths and a list of reverse paths. We set them up now
    # along with dictionaries to allow easy access to all working paths which end in a particular
    # segment.
    forward_working_paths = [[start]]
    reverse_working_paths = [[-end]]

    # Knowing the start/end depth lets us put some limits on how many times a segment can be in a
    # path, which we use to avoid going through loops forever.
    start_seg = graph.segments[abs(start)]
    end_seg = graph.segments[abs(end)]
    start_end_depth = weighted_average(start_seg.depth, end_seg.depth,
                                       start_seg.get_length_no_overlap(graph.overlap),
                                       end_seg.get_length_no_overlap(graph.overlap))

    while True:
        shortest_reverse_path = min(graph.get_path_length(x[1:]) for x in reverse_working_paths)
        reverse_paths_dict = build_path_dictionary(reverse_working_paths)
        forward_working_paths = advance_paths(forward_working_paths, reverse_paths_dict,
                                              shortest_reverse_path, final_paths, False,
                                              sequence, scoring_scheme, expected_scaled_score,
                                              graph, start_end_depth, max_length)
        if not forward_working_paths:
            break

        shortest_forward_path = min(graph.get_path_length(x[1:]) for x in forward_working_paths)
        forward_paths_dict = build_path_dictionary(forward_working_paths)
        reverse_working_paths = advance_paths(reverse_working_paths, forward_paths_dict,
                                              shortest_forward_path, final_paths, True,
                                              reverse_sequence, scoring_scheme,
                                              expected_scaled_score, graph, start_end_depth,
                                              max_length)
        if not reverse_working_paths:
            break

    # Trim the start/end segments, filter for appropriate length and return the final paths!
    final_paths = [list(x)[1:-1] for x in final_paths]
    return [x for x in final_paths if min_length <= graph.get_path_length(x) <= max_length]

#     start_seg = graph.segments[abs(start)]
#     end_seg = graph.segments[abs(end)]
#     start_end_depth = weighted_average(start_seg.depth, end_seg.depth,
#                                        start_seg.get_length_no_overlap(graph.overlap),
#                                        end_seg.get_length_no_overlap(graph.overlap))
#
#     # print('PATH SEARCH FROM START')  # TEMP
#     paths_from_start, best_start_score = \
#         graph.progressive_search_one_direction(start, sequence, scoring_scheme, start_end_depth,
#                                                0.6, max_length, expected_scaled_score)
#
#     # print('\nALL START PATHS:')  # TEMP
#     # for path_from_start in paths_from_start:  # TEMP
#     #     print(' ', path_from_start)  # TEMP
#     # print('BEST START SCORE:', best_start_score)  # TEMP
#
#     # print('PATH SEARCH FROM END')  # TEMP
#     paths_from_end, best_end_score = \
#         graph.progressive_search_one_direction(-end, reverse_complement(sequence),
#                                                scoring_scheme, start_end_depth, 0.6, max_length,
#                                                expected_scaled_score)
#     paths_from_end = [[-x for x in y[::-1]] for y in paths_from_end]  # Flip direction
#
#     # print('\nALL END PATHS:')  # TEMP
#     # for path_from_end in paths_from_end:  # TEMP
#     #     print(' ', path_from_end)  # TEMP
#     # print('BEST END SCORE:', best_end_score)  # TEMP
#
#     joined_paths = graph.combine_paths(paths_from_start, paths_from_end, min_length, max_length)
#
#     # print('\nCOMBINED PATHS:')  # TEMP
#     # for joined_path in joined_paths:  # TEMP
#     #     print(' ', joined_path)  # TEMP
#
#     # If at this point we don't have any valid paths but at least one of the directions had a
#     # decent score, that implies that a real path may exist but we've missed it. We then take
#     # the better scoring direction of the two (implies that one was on the right path) and
#     # try it again, but a bit further.
#     decent_score = 0.95 * expected_scaled_score
#     if not joined_paths and \
#             (best_start_score > decent_score or best_end_score > decent_score):
#         if best_start_score > best_end_score:
#             paths_from_start, best_start_score = \
#                 graph.progressive_search_one_direction(start, sequence, scoring_scheme,
#                                                        start_end_depth, 0.8, max_length,
#                                                        expected_scaled_score)
#         else:  # best_end_score >= best_start_score
#             paths_from_end, best_end_score = \
#                 graph.progressive_search_one_direction(-end, reverse_complement(sequence),
#                                                        scoring_scheme, start_end_depth, 0.8,
#                                                        max_length, expected_scaled_score)
#             paths_from_end = [[-x for x in y[::-1]] for y in paths_from_end]  # Flip direction
#         joined_paths = combine_paths(graph, paths_from_start, paths_from_end, min_length,
#                                      max_length)
#
#         # print('\nCOMBINED PATHS (RETRY):')  # TEMP
#         # for joined_path in joined_paths:  # TEMP
#         #     print(' ', joined_path)  # TEMP
#
#     return joined_paths
#
#
# def progressive_search_one_direction(graph, start, sequence, scoring_scheme, start_end_depth,
#                                      sequence_fraction, max_length, expected_scaled_score):
#     """
#     Searches outward from the start segment, culling paths when they grow too numerous.
#     Returns all found paths which match the given fraction of the sequence. For example, if
#     sequence_fraction is 0.75, this function will try to find a path which matches the first
#     75% of the sequence.
#     """
#     if start not in graph.forward_links:
#         return []
#
#     best_final_score = 0.0
#     target_length = len(sequence) * sequence_fraction
#     # print('TARGET_LENGTH', target_length)  # TEMP
#
#     # When the number of paths exceeds max_working_paths, they are all evaluated and only the
#     # best paths are kept.
#     max_working_paths = settings.PROGRESSIVE_PATH_SEARCH_MAX_WORKING_PATHS
#
#     final_paths = []
#     working_paths = [[x] for x in graph.forward_links[start]]
#
#     while working_paths:
#
#         # Find the length of the shortest working path.
#         shortest_len = min(graph.get_path_length(x) for x in working_paths)
#
#         # Extend the shortest working path(s) by adding downstream segments. Check to see if
#         # this finishes the path or makes it excessively long.
#         new_working_paths = []
#         for path in working_paths:
#             path_len = graph.get_path_length(path)
#
#             # If this path isn't the shortest path, we just save it for later.
#             if path_len > shortest_len:
#                 new_working_paths.append(path)
#                 continue
#
#             # If this path is the shortest path...
#
#             if path[-1] in graph.forward_links:
#                 downstream_segments = graph.forward_links[path[-1]]
#                 for next_seg in downstream_segments:
#                     max_allowed_count = graph.max_path_segment_count(next_seg, start_end_depth)
#                     count_so_far = path.count(next_seg) + path.count(-next_seg)
#                     if count_so_far < max_allowed_count:
#                         extended_path = path + [next_seg]
#                         extended_path_len = graph.get_path_length(extended_path)
#
#                         # If the extended path is excessively long, then it's almost certain
#                         # wrong, and would take a long time to align, so we skip it.
#                         if extended_path_len > max_length:
#                             pass
#
#                         # If the path seems to be long enough, we try to align it against the
#                         # sequence to see if it's actually long enough to be a final path.
#                         elif graph.get_path_length(extended_path) >= target_length:
#                             path_sequence = graph.get_path_sequence(extended_path)
#                             alignment_result = path_alignment(path_sequence, sequence,
#                                                               scoring_scheme, True, 1000)
#                             seqan_parts = alignment_result.split(',', 9)
#                             sequence_end_pos = int(seqan_parts[5])
#                             scaled_score = float(seqan_parts[7])
#
#                             # If the alignment showed that our path has indeed covered the
#                             # required amount of the sequence, then it's finished!
#                             if sequence_end_pos >= target_length:
#                                 final_paths.append((extended_path, scaled_score))
#                                 best_final_score = max(scaled_score, best_final_score)
#                                 # print('FINAL PATH:',  # TEMP
#                                 #       ','.join(str(x) for x in extended_path) + ' (' +  # TEMP
#                                 #       int_to_str(self.get_path_length(extended_path)) + # TEMP
#                                 #       ' bp, score = ' +  # TEMP
#                                 #       float_to_str(scaled_score, 2) + ')')  # TEMP
#                             else:
#                                 new_working_paths.append(extended_path)
#                         else:
#                             new_working_paths.append(extended_path)
#
#         # print('WORKING PATHS: ' + str(len(new_working_paths)))  # TEMP
#
#         # If our number of working paths is still reasonable, we keep them all and continue.
#         if len(new_working_paths) <= max_working_paths:
#             working_paths = new_working_paths
#             continue
#
#         # If we've acquired too many working paths, we must cull out the worst ones to keep
#         # the number manageable.
#
#
#     # We should now have a collection of paths that all cover the necessary amount of the
#     # consensus sequence. Sort them by their score, high to low.
#     final_paths = sorted(final_paths, key=lambda x: x[1], reverse=True)
#     if not final_paths:
#         return []
#
#     # print('\nALL FINAL PATHS:')  # TEMP
#     # for path in final_paths:  # TEMP
#     #     print(' ', path[1], '   ', ','.join(str(x) for x in path[0]))  # TEMP
#     # print('\nBEST FINAL PATH SCORE:', best_final_score)  # TEMP
#
#     # Reduce the final path count to a more reasonable number.
#     score_fraction_threshold = settings.PROGRESSIVE_PATH_SEARCH_SCORE_FRACTION
#     best_final_paths = []
#     while True:
#         best_final_paths = []
#         for final_path in final_paths:
#             score = final_path[1]
#             if score >= best_final_score * score_fraction_threshold:
#                 best_final_paths.append(final_path[0])
#         if len(best_final_paths) > settings.PROGRESSIVE_PATH_SEARCH_DIRECTION_COUNT:
#             score_fraction_threshold = 1.0 - ((1.0 - score_fraction_threshold) / 2)
#         else:
#             break
#
#     # print('\nBEST FINAL PATHS:')  # TEMP
#     # for path in best_final_paths:  # TEMP
#     #     print(' ', ','.join(str(x) for x in path))  # TEMP
#
#     return best_final_paths, best_final_score
#
#
# def combine_paths(graph, paths_from_start, paths_from_end, min_length, max_length):
#     """
#     Returns a list of completed paths made by overlapping the given start and end paths.
#     """
#     # Make every possible combination of start paths and end paths, sorted such that better
#     # paths come first.
#     path_combos = sorted([(x, y) for x in range(len(paths_from_start))
#                           for y in range(len(paths_from_end))], key=lambda z: z[0] + z[1])
#     valid_joined_paths = []
#     for x, y in path_combos:
#         valid_joined_paths += get_overlapping_paths(graph, paths_from_start[x], paths_from_end[y])
#         if len(valid_joined_paths) >= settings.PROGRESSIVE_PATH_SEARCH_FINAL_COUNT:
#             break
#
#     # Remove duplicates.
#     valid_joined_paths.sort()
#     valid_joined_paths = list(valid_joined_paths for valid_joined_paths, _
#                               in itertools.groupby(valid_joined_paths))
#
#     # print('\nALL COMBINED PATHS:')  # TEMP
#     # for valid_joined_path in valid_joined_paths:  # TEMP
#     #     print(' ', valid_joined_path)  # TEMP
#
#     return [x for x in valid_joined_paths
#             if min_length <= graph.get_path_length(x) <= max_length]
#
#
# def get_overlapping_paths(graph, path_1, path_2):
#     """
#     Tries to find all valid overlaps of the two paths. It will search for both perfect
#     overlaps but also for imperfect ones. It returns the paths resulting from all perfect
#     overlaps and the best few imperfect overlaps.
#     """
#     if not path_1 or not path_2:
#         return []
#     overlapping_paths = []
#
#     exact_overlap_count = 0
#     shorter_length = min(len(path_1), len(path_2))
#     longer_length = max(len(path_1), len(path_2))
#
#     # First try no overlap - direct connection.
#     if path_1[-1] in graph.forward_links and path_2[0] in graph.forward_links[path_1[-1]]:
#         overlapping_paths.append((path_1 + path_2, 1.0))
#         exact_overlap_count += 1
#
#     # Now try each possible exact overlap.
#     for overlap in range(1, shorter_length + 1):
#         if path_1[-overlap:] == path_2[:overlap]:
#             overlapping_paths.append((path_1 + path_2[overlap:], 1.0))
#
#     # Now try for inexact overlaps.
#     for overlap in range(1, longer_length + 1):
#         overlap_1 = path_1[-overlap:][:shorter_length]
#         overlap_2 = path_2[:overlap][-shorter_length:]
#         path_2_excess = max(0, overlap - len(path_1))
#         assert len(overlap_1) == len(overlap_2)
#         match_count = sum(1 if overlap_1[i] == overlap_2[i] else 0
#                           for i in range(len(overlap_1)))
#         match_fraction = match_count / overlap
#         if match_fraction == 1.0 and overlap <= shorter_length:
#             continue
#         if match_fraction > 0.0:
#             capped_overlap = min(overlap, shorter_length)
#             midpoint = capped_overlap // 2
#             overlap_positions = [midpoint]
#             for i in range(1, capped_overlap - midpoint + 1):
#                 if midpoint + i < capped_overlap:
#                     overlap_positions.append(midpoint + i)
#                 if midpoint - i >= 0:
#                     overlap_positions.append(midpoint - i)
#             for pos in overlap_positions:
#                 pos_1 = len(path_1) - overlap + path_2_excess + pos
#                 pos_2 = pos + path_2_excess
#                 if path_1[pos_1] == path_2[pos_2]:
#                     overlapping_paths.append((path_1[:pos_1] + path_2[pos_2:], match_fraction))
#                     break
#
#     # Return only the most matching overlapping paths.
#     overlapping_paths = sorted(overlapping_paths, key=lambda x: x[1], reverse=True)
#     return [x[0] for x in overlapping_paths[:exact_overlap_count + 5]]


def build_path_dictionary(path_list):
    """
    Constructs a dictionary where the key is the furthest segment in the path and the value is a
    list of paths which end in that segment. The paths are reversed because they'll be used by the
    paths coming from the opposite direction.
    """
    path_dict = defaultdict(list)
    for path in path_list:
        r_path = reverse_path(path)
        path_dict[r_path[0]].append(r_path)
    return path_dict


def reverse_path(path):
    """
    Reverses the order and sign of path.
    """
    return [-x for x in path[::-1]]


def advance_paths(working_paths, opposite_paths_dict, shortest_opposite_path,
                  final_paths, flip_new_final_paths, sequence, scoring_scheme,
                  expected_scaled_score, graph, start_end_depth, total_max_length):
    """
    This function takes the working paths for one direction and extends them until there are too
    many or there are no more.
    """
    # For this function, the longest we'll allow paths to get is the the max length minus how far
    # the other side has gotten.
    max_length = total_max_length - shortest_opposite_path

    while True:
        # If the working paths have run out or grown too large, then we're finished with this
        # round of advancing.
        if not 0 < len(working_paths) <= settings.PROGRESSIVE_PATH_SEARCH_MAX_WORKING_PATHS:
            break

        shortest_path_len = min(graph.get_path_length(x) for x in working_paths)

        # Extend the shortest working path(s) by adding downstream segments.
        new_working_paths = []
        for path in working_paths:
            path_len = graph.get_path_length(path)

            # If this path isn't the shortest path, we don't deal with it this time.
            if path_len > shortest_path_len:
                new_working_paths.append(path)

            # If it is the shortest path and has downstream segments...
            elif path[-1] in graph.forward_links:
                downstream_segments = graph.forward_links[path[-1]]
                for next_seg in downstream_segments:

                    # Make sure we haven't already used this segment too many times in the path.
                    max_allowed_count = graph.max_path_segment_count(next_seg, start_end_depth)
                    count_so_far = path.count(next_seg) + path.count(-next_seg)
                    if count_so_far < max_allowed_count:

                        # If the next segment is in the dictionary of the opposite direction's
                        # paths, that means we've found a path through to the other side!
                        if next_seg in opposite_paths_dict:
                            for final_part in opposite_paths_dict[next_seg]:
                                final_path = path + final_part
                                if flip_new_final_paths:
                                    final_path = reverse_path(final_path)
                                final_paths.add(tuple(final_path))

                        # Finally, extend the path if doing so won't make it too long.
                        if graph.get_path_length(path[1:] + [next_seg]) <= max_length:
                            new_working_paths.append(path + [next_seg])

        working_paths = new_working_paths

    # If we've exceeded the allowable working count, cull the paths down to size now.
    if len(working_paths) > settings.PROGRESSIVE_PATH_SEARCH_MAX_WORKING_PATHS:
        working_paths = cull_paths(graph, working_paths, sequence, scoring_scheme,
                                   expected_scaled_score)

    return working_paths


def cull_paths(graph, paths, sequence, scoring_scheme, expected_scaled_score):
    """
    Returns a reduced list of paths - the ones which best align to the given sequence.
    """
    path_count_before_cull = len(paths)

    # It's possible that all of the working paths share quite a bit in common at their
    # start. We can therefore find the common starting sequence and align to that once,
    # and then only do separate alignments for the remainder of the paths, saving some time.
    common_start = []
    smallest_seg_count = min(len(x) for x in paths)
    for i in range(smallest_seg_count):
        potential_common_seg = paths[0][i]
        for path in paths:
            if path[i] != potential_common_seg:
                break
        else:
            common_start.append(potential_common_seg)
            continue
        break
    common_start = common_start[1:]  # Remove starting segment (not part of consensus)
    common_path_seq = graph.get_path_sequence(common_start)[:-100]
    path_align_start = len(common_path_seq)
    seq_align_start = 0
    if common_path_seq:
        alignment_result = path_alignment(common_path_seq, sequence, scoring_scheme, True, 1000)
        seq_align_start = int(alignment_result.split(',', 6)[5])

    scored_paths = []
    shortest_len = min(graph.get_path_length(x) for x in paths)
    seq_after_common_path = sequence[seq_align_start:]
    for path in paths:
        path = path[1:]  # Remove starting segment (not part of consensus)
        path_seq_after_common_path = graph.get_path_sequence(path)[path_align_start:shortest_len]
        alignment_result = path_alignment(path_seq_after_common_path, seq_after_common_path,
                                          scoring_scheme, True, 500)
        if alignment_result:
            scaled_score = float(alignment_result.split(',', 8)[7])
            scored_paths.append((path, scaled_score))

    scored_paths = sorted(scored_paths, key=lambda x: x[1], reverse=True)
    if not scored_paths:
        return []
    best_score = scored_paths[0][1]

    # If all of the scores have dropped well below our expectation, then our path finding has
    # probably taken a wrong turn and we should give up!
    if best_score < 0.9 * expected_scaled_score:
        return []

    # Now that each path is scored we keep the ones that are closest in score to the
    # best one. For example, if settings.PROGRESSIVE_PATH_SEARCH_SCORE_FRACTION is
    # 0.99, then any path which has 99% or more of the best score is kept. But if this
    # approach still results in too many surviving paths, then we increase the score
    # fraction threshold and try again.
    score_fraction_threshold = settings.PROGRESSIVE_PATH_SEARCH_SCORE_FRACTION
    surviving_paths = []
    while True:
        surviving_paths = []
        for scored_path in scored_paths:
            score = scored_path[1]
            if score >= best_score * score_fraction_threshold:
                surviving_paths.append(scored_path)
        if len(surviving_paths) > settings.PROGRESSIVE_PATH_SEARCH_MAX_WORKING_PATHS // 2:
            score_fraction_threshold = 1.0 - ((1.0 - score_fraction_threshold) / 2)
        else:
            break
    paths = list(x[0] for x in surviving_paths)

    # If the cull failed to reduce the number of paths whatsoever, that's not good!
    # We can't let the paths grow forever, so we must chop them down, even if we have
    # to do so arbitrarily.
    path_count_after_cull = len(paths)
    if path_count_after_cull == path_count_before_cull:
        paths = paths[:len(paths) // 2]

    return paths
