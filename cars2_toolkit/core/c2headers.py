"""Header namespace of an Avalanche SubNetwork (Cars 2 / Toy Story 3 era Tupperware).

HeaderStrings        unique name strings
HeaderStringIndices  name of id k+1  ->  HeaderStrings[HeaderStringIndices[k]]
HeaderLayout         flat (parent_id, child_count) pairs in depth-first pre-order.
                     Root is the virtual id 0; each pair gives its parent the next
                     child_count fresh ids (ids start at 1).
Every DataNode / ProcessorNode 'Header' is such an id, i.e. a path like
Anchor/Main_Ctrl/RotateFront/ClipProcessor.
"""

# v2.0.21: a shipping file can give one parent two children of the same name
# (King: two 'Anchor/Null' branches, each with its own 'Transform' = the right
# exhaust markers its porter renamed). They are separate header ids. To keep
# node paths unique while editing, the 2nd+ such child is named
# '<name>' + ALIAS + '<n>' inside the toolkit; encode_paths writes the plain
# name again, so the file keeps its separate same-named entries.
ALIAS = '␟'


def plain_name(name):
    return name.split(ALIAS, 1)[0]


def decode_paths_unique(strings, indices, layout):
    """decode_paths, with a repeated sibling name aliased (see ALIAS). Same as
    decode_paths for every file without repeats."""
    n = len(indices)
    parent = [None] * (n + 1)
    nxt = 1
    for i in range(0, len(layout), 2):
        p, c = layout[i], layout[i + 1]
        for _ in range(c):
            parent[nxt] = p
            nxt += 1
    if nxt != n + 1:
        raise ValueError('layout assigns %d ids, table has %d' % (nxt - 1, n))
    paths = [None] * (n + 1)
    seen = {}
    for h in range(1, n + 1):
        name = strings[indices[h - 1]]
        p = parent[h]
        k = seen.get((p, name), 0)
        seen[(p, name)] = k + 1
        if k:
            name = '%s%s%d' % (name, ALIAS, k)
        paths[h] = name if p == 0 else paths[p] + '/' + name
    return paths[1:]


def decode_paths(strings, indices, layout):
    """-> list of paths, paths[id-1] for header id."""
    n = len(indices)
    parent = [None] * (n + 1)
    nxt = 1
    for i in range(0, len(layout), 2):
        p, c = layout[i], layout[i + 1]
        for _ in range(c):
            parent[nxt] = p
            nxt += 1
    if nxt != n + 1:
        raise ValueError('layout assigns %d ids, table has %d' % (nxt - 1, n))
    paths = [None] * (n + 1)
    for h in range(1, n + 1):
        name = strings[indices[h - 1]]
        p = parent[h]
        paths[h] = name if p == 0 else paths[p] + '/' + name
    return paths[1:]


def encode_paths(paths):
    """Inverse of decode_paths. `paths` in id order (paths[0] is id 1).

    Ids must be consistent with the depth-first allocation rule; use
    allocate() to produce a valid ordering from an arbitrary set of paths.
    Returns (strings, indices, layout)."""
    pid = {p: i + 1 for i, p in enumerate(paths)}
    kids = {0: []}
    for i, p in enumerate(paths):
        par = pid[p.rsplit('/', 1)[0]] if '/' in p else 0
        kids.setdefault(par, []).append(i + 1)
    layout = []
    expect = [1]
    def visit(node):
        ch = kids.get(node)
        if not ch:
            return
        if ch != list(range(expect[0], expect[0] + len(ch))):
            raise ValueError('ids of children of %r are not the next fresh ids' % node)
        expect[0] += len(ch)
        layout.extend((node, len(ch)))
        for c in ch:
            visit(c)
    visit(0)
    strings, sidx, indices = [], {}, []
    for p in paths:
        raw = p.rsplit('/', 1)[-1]
        if raw not in sidx:              # an aliased repeat keeps its own string entry, as in the file (King)
            sidx[raw] = len(strings); strings.append(plain_name(raw))
        indices.append(sidx[raw])
    return strings, indices, layout


def allocate(paths):
    """Assign header ids to an arbitrary collection of paths (parents added
    automatically). Sibling order = first-seen order. Returns ordered path list
    (id = index + 1)."""
    children = {'': []}
    seen = set()
    for p in paths:
        parts = p.split('/')
        for i in range(len(parts)):
            q = '/'.join(parts[:i + 1])
            if q in seen:
                continue
            seen.add(q)
            par = '/'.join(parts[:i])
            children.setdefault(par, []).append(q)
    out = []
    def visit(node):
        ch = children.get(node, [])
        out.extend(ch)
        for c in ch:
            visit(c)
    visit('')
    return out
