'''
TACO: Transcriptome meta-assembly from RNA-Seq
'''
import logging
import collections
import networkx as nx

from base import Exon, Strand
from splice_graph import split_transfrag
from optimize import maximize_bisect

__author__ = "Matthew Iyer and Yashar Niknafs"
__copyright__ = "Copyright 2016"
__credits__ = ["Matthew Iyer", "Yashar Niknafs"]
__license__ = "GPL"
__version__ = "0.4.0"
__maintainer__ = "Yashar Niknafs"
__email__ = "yniknafs@umich.edu"
__status__ = "Development"


# graph attributes
SOURCE = -1
SINK = -2
KMER_EXPR = 'expr'
SMOOTH_FWD = 'smfwd'
SMOOTH_REV = 'smrev'
SMOOTH_TMP = 'smtmp'


def init_node_attrs():
    return {KMER_EXPR: 0.0, SMOOTH_FWD: 0.0,
            SMOOTH_REV: 0.0, SMOOTH_TMP: 0.0}


def smooth_iteration(G, expr_attr, smooth_attr):
    nodes = nx.topological_sort(G)
    for u in nodes:
        ud = G.node[u]
        smooth_expr = ud[smooth_attr]
        succ = G.successors(u)
        if len(succ) == 0:
            continue
        total_nbr_expr = sum(G.node[v][expr_attr] for v in succ)
        if total_nbr_expr == 0:
            # if all successors have zero score apply smoothing evenly
            avg_expr = smooth_expr / len(succ)
            for v in succ:
                vd = G.node[v]
                vd[SMOOTH_TMP] += avg_expr
                vd[smooth_attr] += avg_expr
        else:
            # apply smoothing proportionately
            for v in succ:
                vd = G.node[v]
                frac = vd[expr_attr]/float(total_nbr_expr)
                adj_expr = frac * smooth_expr
                vd[SMOOTH_TMP] += adj_expr
                vd[smooth_attr] += adj_expr


def smooth_graph(G, expr_attr=KMER_EXPR):
    # smooth in forward direction
    smooth_iteration(G, expr_attr, SMOOTH_FWD)
    # smooth in reverse direction
    G.reverse(copy=False)
    smooth_iteration(G, expr_attr, SMOOTH_REV)
    G.reverse(copy=False)
    # apply densities to nodes
    for n, d in G.nodes_iter(data=True):
        d[expr_attr] += d[SMOOTH_TMP]


def add_path(K, path, expr):
    # add first kmer
    from_id = path[0]
    if from_id not in K:
        K.add_node(from_id, attr_dict=init_node_attrs())
    kmerattrs = K.node[from_id]
    kmerattrs[KMER_EXPR] += expr
    # the first kmer should be "smoothed" in reverse direction
    kmerattrs[SMOOTH_REV] += expr
    for to_id in path[1:]:
        if to_id not in K:
            K.add_node(to_id, attr_dict=init_node_attrs())
        kmerattrs = K.node[to_id]
        kmerattrs[KMER_EXPR] += expr
        # connect kmers
        K.add_edge(from_id, to_id)
        # update from_kmer to continue loop
        from_id = to_id
    # the last kmer should be "smoothed" in forward direction
    kmerattrs[SMOOTH_FWD] += expr


def hash_kmers(id_kmer_map, k, ksmall):
    kmer_hash = collections.defaultdict(lambda: set())
    for kmer_id, kmer in id_kmer_map.iteritems():
        for i in xrange(k - (ksmall-1)):
            kmer_hash[kmer[i:i+ksmall]].add(kmer_id)
    return kmer_hash


def find_short_path_kmers(kmer_hash, K, path, expr):
    """
    find kmers where 'path' is a subset and partition 'expr'
    of path proportionally among all matching kmers

    generator function yields (kmer_id, expr) tuples
    """
    if path not in kmer_hash:
        return
    matching_kmers = []
    total_expr = 0.0
    for kmer_id in kmer_hash[path]:
        # compute total expr at matching kmers
        kmer_expr = K.node[kmer_id][KMER_EXPR]
        total_expr += kmer_expr
        matching_kmers.append((kmer_id, kmer_expr))
    # now calculate fractional densities for matching kmers
    for kmer_id, kmer_expr in matching_kmers:
        if total_expr == 0:
            new_expr = expr / len(matching_kmers)
        else:
            new_expr = expr * (kmer_expr / total_expr)
        yield ([kmer_id], new_expr)


def get_unreachable_kmers(K, source=None, sink=None):
    '''
    Path graphs created with k > 2 can yield fragmented paths. Test for
    these by finding unreachable kmers from source or sink
    '''
    if source is None:
        source = SOURCE
    if sink is None:
        sink = SINK
    allnodes = set(K)
    # unreachable from source
    a = allnodes - set(nx.shortest_path_length(K, source=source).keys())
    # unreachable from sink
    b = allnodes - set(nx.shortest_path_length(K, target=sink).keys())
    return a | b


def is_graph_valid(K):
    if SOURCE not in K:
        return False
    if SINK not in K:
        return False
    return nx.has_path(K, SOURCE, SINK)


def get_kmers(path, k):
    for i in xrange(0, len(path) - (k-1)):
        yield path[i:i+k]


def get_path(sgraph, t):
    nodes = [Exon(*n) for n in split_transfrag(t, sgraph.node_bounds)]
    if sgraph.strand == Strand.NEG:
        nodes.reverse()
    return tuple(nodes)


def get_node_lengths(sgraph, t):
    return [(n[1]-n[0]) for n in split_transfrag(t, sgraph.node_bounds)]


def find_longest_path(sgraph):
    kmax = 1
    for t in sgraph.itertransfrags():
        path_length = sum(1 for n in split_transfrag(t, sgraph.node_bounds))
        kmax = max(kmax, path_length)
    return kmax


def create_path_graph(sgraph, k):
    # initialize path graph
    K = nx.DiGraph()
    K.add_node(SOURCE, attr_dict=init_node_attrs())
    K.add_node(SINK, attr_dict=init_node_attrs())
    # find all beginning/end nodes in splice graph
    start_nodes, stop_nodes = sgraph.get_start_stop_nodes()
    # convert paths to k-mers and create a k-mer to integer node map
    kmer_id_map = {}
    id_kmer_map = {}
    short_transfrags = []
    kmer_paths = []
    current_id = 0
    for t in sgraph.itertransfrags():
        # get nodes
        path = get_path(sgraph, t)
        # check for start and stop nodes
        is_start = (path[0] in start_nodes)
        is_end = (path[-1] in stop_nodes)
        full_length = is_start and is_end
        if (len(path) < k) and (not full_length):
            short_transfrags.append((t, path))
            continue
        # convert to path of kmers
        kmer_path = []
        if is_start:
            kmer_path.append(SOURCE)
        if len(path) < k:
            # specially add short full length paths because
            # they are not long enough to have kmers
            kmers = [path]
        else:
            kmers = get_kmers(path, k)
        # convert to path of kmers
        for kmer in kmers:
            if kmer not in kmer_id_map:
                kmer_id = current_id
                kmer_id_map[kmer] = current_id
                id_kmer_map[current_id] = kmer
                current_id += 1
            else:
                kmer_id = kmer_id_map[kmer]
            kmer_path.append(kmer_id)
        if is_end:
            kmer_path.append(SINK)
        kmer_paths.append((kmer_path, t.expr))
    # add paths to graph
    for path, expr in kmer_paths:
        add_path(K, path, expr)
    # remove nodes that are unreachable from the source or sink, these occur
    # due to fragmentation when k > 2
    lost_kmers = {}
    for kmer in get_unreachable_kmers(K, SOURCE, SINK):
        if (kmer == SOURCE) or (kmer == SINK):
            continue
        lost_kmers[kmer] = K.node[kmer][KMER_EXPR]
        del id_kmer_map[kmer]
        K.remove_node(kmer)
    # graph is invalid if there is no path from source to sink
    valid = is_graph_valid(K)
    # add graph attributes
    K.graph['k'] = k
    K.graph['source'] = SOURCE
    K.graph['sink'] = SINK
    K.graph['id_kmer_map'] = id_kmer_map
    K.graph['short_transfrags'] = short_transfrags
    K.graph['lost_kmers'] = lost_kmers
    K.graph['valid'] = valid
    return K


def rescue_short_transfrags(K):
    '''create kmer graph from partial paths'''
    k = K.graph['k']
    id_kmer_map = K.graph['id_kmer_map']
    short_transfrag_dict = collections.defaultdict(list)
    for t, path in K.graph['short_transfrags']:
        # save fragmented short paths
        short_transfrag_dict[len(path)].append((t, path))
    # try to add short paths to graph if they are exact subpaths of
    # existing kmers
    kmer_paths = []
    lost_transfrags = []
    for ksmall, short_transfrag_paths in short_transfrag_dict.iteritems():
        kmer_hash = hash_kmers(id_kmer_map, k, ksmall)
        for t, path in short_transfrag_paths:
            matching_paths = \
                list(find_short_path_kmers(kmer_hash, K, path, t.expr))
            if len(matching_paths) == 0:
                lost_transfrags.append((t, path))
            kmer_paths.extend(matching_paths)
    # add new paths
    for path, expr in kmer_paths:
        add_path(K, path, expr)
    # add graph attributes
    K.graph['lost_transfrags'] = lost_transfrags


def get_reachable_nodes(K):
    # keep track of all reachable nodes
    id_kmer_map = K.graph['id_kmer_map']
    reachable_nodes = set()
    for kmer in K.nodes_iter():
        if kmer == SOURCE or kmer == SINK:
            continue
        reachable_nodes.update(id_kmer_map[kmer])
    return reachable_nodes


def get_lost_nodes(sgraph, K):
    return set(sgraph.G) - get_reachable_nodes(K)


def create_optimal_path_graph(sgraph, frag_length=400, kmax=0,
                              loss_threshold=0.10, stats_fh=None):
    '''
    create a path graph from the original splice graph using paths of length
    'k' for assembly. The parameter 'k' will be chosen by maximizing the
    number of reachable k-mers in the path graph while tolerating at most
    'loss_threshold' percent of expression.
    '''
    # find upper bound to k
    user_kmax = kmax
    kmax = find_longest_path(sgraph)
    if user_kmax > 0:
        # user can force a specific kmax (for debugging/testing purposes)
        kmax = min(user_kmax, kmax)
    sgraph_id_str = '%s:%d-%d[%s]' % (sgraph.chrom, sgraph.start, sgraph.end,
                                      Strand.to_gtf(sgraph.strand))
    tot_expr = sum(sgraph.get_expr_data(*n).mean() for n in sgraph.G)

    def compute_kmers(k):
        K = create_path_graph(sgraph, k)
        valid = K.graph['valid']
        short_transfrags = K.graph['short_transfrags']
        lost_kmers = K.graph['lost_kmers']
        lost_nodes = get_lost_nodes(sgraph, K)
        lost_expr = sum(sgraph.get_expr_data(*n).mean() for n in lost_nodes)
        lost_expr_frac = 0.0 if tot_expr == 0 else lost_expr / tot_expr
        logging.debug('%s k=%d kmax=%d t=%d n=%d kmers=%d short_transfrags=%d '
                      'lost_kmers=%d tot_expr=%.3f lost_expr=%.3f '
                      'lost_expr_frac=%.3f valid=%d' %
                      (sgraph_id_str, k, kmax, len(sgraph.transfrags),
                       len(sgraph.G), len(K), len(short_transfrags),
                       len(lost_kmers), tot_expr, lost_expr,
                       lost_expr_frac, int(valid)))
        if not valid:
            return -k
        if lost_expr_frac > loss_threshold:
            return -k
        return len(K)

    k, num_kmers = maximize_bisect(compute_kmers, 1, kmax, 0)
    K = create_path_graph(sgraph, k)
    rescue_short_transfrags(K)
    return K, k


def reconstruct_path(kmer_path, id_kmer_map, strand):
    # reconstruct path from kmer ids
    path = list(id_kmer_map[kmer_path[1]])
    path.extend(id_kmer_map[n][-1] for n in kmer_path[2:-1])
    # reverse negative stranded data so that all paths go from
    # small -> large genomic coords
    if strand == Strand.NEG:
        path.reverse()
    # collapse contiguous nodes along path
    newpath = []
    chain = [path[0]]
    for v in path[1:]:
        if chain[-1].end != v.start:
            # update path with merge chain node
            newpath.append(Exon(chain[0].start,
                                chain[-1].end))
            # reset chain
            chain = []
        chain.append(v)
    # add last chain
    newpath.append(Exon(chain[0].start, chain[-1].end))
    return newpath
