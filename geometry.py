"""Blender meshes -> Cars 2 character geometry (feeds core/c2geom.rebuild).

Every mesh object under the character armature is exported, split by
material. Materials must be the character's own (imported) materials - each
maps to a MaterialPool entry. Bones that carry weights but are not yet skin
bones become new skin bones; they must be in the game rig first ("Add Selected
Bones to Rig"), because the game moves a skin bone through its rig node.
"""
import json

import bpy

from . import character as ch

PROP_MAT = 'cars2_material'          # on materials: MaterialPool index
PROP_TEMPLATES = 'cars2_mesh_nodes'  # on the armature: {material index: [scene-tree node indices]}


def tag_import(arm, materials, tree):
    """Called right after import: remember which game material each Blender
    material is and which scene-tree mesh nodes used it."""
    for i, mat in enumerate(materials or []):
        if mat is not None:
            mat[PROP_MAT] = i
    nodes = {}
    for i, n in enumerate(tree['SceneTreeNodePool'].values()):
        if n['Type'] in ('SubGeometryLit', 'SubGeometry'):
            nodes.setdefault(str(n['MaterialReference']), []).append(i)
    arm[PROP_TEMPLATES] = json.dumps(nodes)


ORIG = ('cars2_pos', 'cars2_normal', 'cars2_tangent', 'cars2_binormal')
ORIG_UV = 'cars2_uv'


def store_originals(arm, tree, vb, ib):
    """Keep each imported vertex's exact game data (normal, tangent, binormal,
    UV) as hidden attributes, so untouched vertices export bit-for-bit.
    Blender's own normals are only used for vertices that were moved or added.
    The importer builds one object per material, vertices = each mesh node's
    used vertices in index order, concatenated in scene-tree order - the same
    order c2geom.extract_meshes produces."""
    import c2geom
    per_mat = {}
    for m in c2geom.extract_meshes(tree, vb, ib):
        per_mat.setdefault(m['material'], []).extend(m['verts'])
    stored = 0
    for obj in character_meshes(arm):
        slot = obj.material_slots[0].material if obj.material_slots else None
        if slot is None or PROP_MAT not in slot.keys():
            continue
        verts = per_mat.get(int(slot[PROP_MAT]))
        me = obj.data
        if not verts or len(verts) != len(me.vertices):
            continue
        for name, key, size in (('cars2_pos', 'Position', 3), ('cars2_normal', 'Normal', 3),
                                ('cars2_tangent', 'Tangent', 3), ('cars2_binormal', 'Binormal', 3)):
            if key not in verts[0]:
                continue
            a = me.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
            a.data.foreach_set('vector', [c for v in verts for c in tuple(v[key])[:size]])
        if 'Uv1' in verts[0]:
            a = me.attributes.new(ORIG_UV, 'FLOAT2', 'POINT')
            a.data.foreach_set('vector', [c for v in verts for c in v['Uv1']])
        stored += 1
    return stored


def character_meshes(arm):
    out = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.parent == arm or any(m.type == 'ARMATURE' and m.object == arm for m in obj.modifiers):
            out.append(obj)
    return sorted(out, key=lambda o: o.name)


def _corner_normals(me):
    if hasattr(me, 'corner_normals'):                       # Blender 4.1+
        return [cn.vector.copy() for cn in me.corner_normals]
    me.calc_normals_split()
    return [lp.normal.copy() for lp in me.loops]


def mesh_with_modifiers(obj, warnings):
    """obj's mesh as the viewport shows it at rest: every enabled modifier but
    Armature applied (Smooth by Angle, Weighted Normal, Mirror ...), so the
    shading you set is what gets exported. -> (mesh, free); call free() when done."""
    if not any(m.type != 'ARMATURE' and m.show_viewport for m in obj.modifiers):
        return obj.data, lambda: None
    # the game skins the mesh itself: evaluate without the Armature modifier
    arms = [m for m in obj.modifiers if m.type == 'ARMATURE' and m.show_viewport]
    for m in arms:
        m.show_viewport = False
    try:
        ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        me = ev.to_mesh()
    finally:
        for m in arms:
            m.show_viewport = True
    if obj.vertex_groups and not any(v.groups for v in me.vertices):
        warnings.append('%s: its modifiers drop the bone weights - apply them first' % obj.name)
    return me, ev.to_mesh_clear


def collect(arm, tree, problems, warnings=None):
    """-> {'meshes': [...], 'new_bones': [...]} for c2geom.rebuild, or None."""
    st = list(tree['SceneTreeNodePool'].values())
    skin = {n['NodeName'] for n in st if n['Type'] == 'Bone'}
    templates = {int(k): v for k, v in json.loads(arm.get(PROP_TEMPLATES, '{}')).items()}
    any_template = next((v[0] for v in templates.values()), None)
    if any_template is None:
        problems.append('this character has no mesh nodes to use as templates')
        return None
    vs = list(tree['VertexStreamPool'].values())

    def layout_names(node_index):
        return {e['Name'] for k in st[node_index]['VertexStreamReferences'] for e in vs[k]['Elements'].values()}

    # every mesh gets the Tangent + Normal + Binormal stream (like the body and
    # tires): the layout of a character mesh that has it; node settings are the
    # same either way, the stream declares the format
    tangent_node = next((n for nodes in templates.values() for n in nodes
                         if {'Tangent', 'Binormal', 'Normal'} <= layout_names(n)), None)

    warnings = warnings if warnings is not None else []
    unweighted = 0
    mapping = ch.bone_nodes(arm)
    to_arm = arm.matrix_world.inverted()
    meshes, weighted = [], set()
    slots_used = set()
    for obj in character_meshes(arm):
        other_mods = [m.name for m in obj.modifiers if m.type != 'ARMATURE' and m.show_viewport]
        if other_mods:
            warnings.append('%s: modifiers %s are ignored (apply them first)' % (obj.name, other_mods))
        me = obj.data
        M = to_arm @ obj.matrix_world
        N3 = M.to_3x3().inverted().transposed()
        me.calc_loop_triangles()
        normals = _corner_normals(me)
        uv = me.uv_layers.active
        groups = {g.index: g.name for g in obj.vertex_groups}
        by_mat = {}
        for tri in me.loop_triangles:
            by_mat.setdefault(tri.material_index, []).append(tri)
        for mi, tris in sorted(by_mat.items()):
            mat = obj.material_slots[mi].material if mi < len(obj.material_slots) else None
            if mat is None or PROP_MAT not in mat.keys():
                problems.append('%s: faces use %s, which is not one of the character materials'
                                % (obj.name, mat.name if mat else 'no material'))
                continue
            game_mat = int(mat[PROP_MAT])
            cands = templates.get(game_mat) or [any_template]
            # v2.0.34: only the material's own mesh nodes are taken over. A material without one (Barry's
            # unused Barry_Chrome) borrows a node's settings as its template but gets a new node: before, a
            # mesh of it whose object name sorted before the body took the body's node
            node = next((c for c in templates.get(game_mat, ()) if c not in slots_used), None)
            template = cands[0]
            layout_node = tangent_node if tangent_node is not None else template
            needs = layout_names(layout_node)
            if 'Tangent' in needs:
                if uv is None:
                    problems.append('%s: needs a UV map (its layout stores tangents)' % obj.name)
                    continue
                uv_name = uv.name
                me.calc_tangents(uvmap=uv_name)
                # calc_tangents reallocates loop data: earlier UV-layer references go stale
                # (they read tangent memory, even .name) - fetch the layer again by its saved name
                uv = me.uv_layers[uv_name]
            if 'Uv1' in needs and uv is None:
                problems.append('%s: has no UV map' % obj.name)
                continue
            orig = {n: me.attributes[n].data for n in ORIG + (ORIG_UV,) if n in me.attributes}
            verts, index_of, indices = [], {}, []
            for tri in tris:
                for li in tri.loops:
                    lp = me.loops[li]
                    vi = lp.vertex_index
                    p = M @ me.vertices[vi].co
                    n = (N3 @ normals[li]).normalized()
                    u = tuple(uv.data[li].uv) if uv else (0.0, 0.0)
                    exact = _original(orig, vi, me.vertices[vi].co, u)
                    if exact is not None:            # untouched vertex: the game's own data, bit for bit
                        key = (vi, 'orig')
                        k = index_of.get(key)
                        if k is None:
                            v = dict(exact)
                            v['Position'] = tuple(p)
                            if 'Tangent' in needs and 'Tangent' not in v:
                                # the game's data had no tangents (a Normal-only mesh,
                                # given the tangent layout now): computed from the mesh
                                tg = (M.to_3x3() @ lp.tangent).normalized()
                                b = -lp.bitangent_sign * n.cross(tg)
                                v['Tangent'], v['Binormal'] = tuple(tg), tuple(b.normalized())
                            v['_bones'] = _weights(me, vi, groups, skin, mapping)
                            if not v['_bones']:
                                v['_bones'] = [(_fallback_bone(arm, skin), 1.0)]
                                unweighted += 1
                            weighted.update(b_ for b_, _ in v['_bones'])
                            k = len(verts)
                            verts.append(v)
                            index_of[key] = k
                        indices.append(k)
                        continue
                    key = (vi, round(u[0], 5), round(u[1], 5), round(n.x, 4), round(n.y, 4), round(n.z, 4))
                    if 'Tangent' in needs:
                        tg = (M.to_3x3() @ lp.tangent).normalized()
                        key += (round(tg.x, 4), round(tg.y, 4), round(tg.z, 4), lp.bitangent_sign)
                    k = index_of.get(key)
                    if k is None:
                        v = {'Position': tuple(p), 'Normal': tuple(n), 'Uv1': (u[0], 1.0 - u[1])}
                        if 'Tangent' in needs:
                            b = -lp.bitangent_sign * n.cross(tg)
                            v['Tangent'], v['Binormal'] = tuple(tg), tuple(b.normalized())
                        bw = _weights(me, vi, groups, skin, mapping)
                        if not bw:
                            bw = [(_fallback_bone(arm, skin), 1.0)]
                            unweighted += 1
                        weighted.update(b_ for b_, _ in bw)
                        v['_bones'] = bw
                        k = len(verts)
                        verts.append(v)
                        index_of[key] = k
                    indices.append(k)
            if 'Tangent' in needs:
                me.free_tangents()
            if not indices:
                continue
            name = st[node]['NodeName'] if node is not None and _is_import_object(obj, game_mat) else \
                '%s_%s' % (obj.name, mat.name)
            m = {'name': name, 'material': game_mat, 'template': template, 'verts': verts, 'indices': indices}
            if layout_node != template:
                m['layout'] = layout_node
            if node is not None and _is_import_object(obj, game_mat):
                m['node'] = node
                slots_used.add(node)
            meshes.append(m)

    if unweighted:
        warnings.append('%d vertices had no bone weights and were put on %s'
                        % (unweighted, _fallback_bone(arm, skin)))
    new_bones = []
    for b in sorted(weighted - skin, key=lambda n: len(arm.data.bones[n].parent_recursive)):
        bone = arm.data.bones[b]
        if b not in mapping:
            problems.append('vertices are weighted to %s, which is not in the game rig '
                            '(select it and press Add Selected Bones to Rig)' % b)
            continue
        parent = next((p.name for p in bone.parent_recursive if p.name in skin
                       or any(nb['name'] == p.name for nb in new_bones)), None)
        if parent is None:
            problems.append('%s: no skin bone above it to attach to' % b)
            continue
        m4 = bone.matrix_local.transposed()                       # row-vector layout
        new_bones.append({'name': b, 'parent': parent, 'matrix': [m4[r][c] for r in range(4) for c in range(4)]})
    return {'meshes': meshes, 'new_bones': new_bones}


def _weights(me, vi, groups, skin, mapping):
    bw = [(groups[g.group], g.weight) for g in me.vertices[vi].groups if g.group in groups and g.weight > 0]
    return [(b, w) for b, w in bw if b in skin or b in mapping]


def _original(orig, vi, co, uv):
    """The stored game data for vertex vi, if the vertex is untouched
    (same position, same UV); else None."""
    if 'cars2_pos' not in orig or 'cars2_normal' not in orig:
        return None
    p = orig['cars2_pos'][vi].vector
    if max(abs(p[k] - co[k]) for k in range(3)) > 1e-6:
        return None
    out = {'Normal': tuple(orig['cars2_normal'][vi].vector) + (0.0,)}
    if 'cars2_uv' in orig:
        u = orig['cars2_uv'][vi].vector
        if abs(u[0] - uv[0]) > 1e-4 or abs((1.0 - u[1]) - uv[1]) > 1e-4:
            return None
        out['Uv1'] = (u[0], u[1])
    if 'cars2_tangent' in orig:
        out['Tangent'] = tuple(orig['cars2_tangent'][vi].vector) + (0.0,)
        out['Binormal'] = tuple(orig['cars2_binormal'][vi].vector) + (0.0,)
    return out


def _is_import_object(obj, game_mat):
    """Imported objects are named after their material and hold that material first."""
    return len(obj.material_slots) > 0 and obj.material_slots[0].material is not None \
        and obj.material_slots[0].material.get(PROP_MAT) == game_mat


def _fallback_bone(arm, skin):
    """Unweighted vertices ride on the body (the game needs a bone per vertex)."""
    for name in ('Body', 'Main'):
        if name in skin:
            return name
    return sorted(skin)[0]


def prepare(arm, problems, warnings):
    """Geometry for export from the scene's meshes (None if there are errors)."""
    import c2oct
    tree = c2oct.load(arm[ch.PROP_SCENE])
    geom = collect(arm, tree, problems, warnings)
    return None if problems else geom
