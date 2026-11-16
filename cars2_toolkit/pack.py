"""Pack an exported character as the game's character archive (public 2.0.8).

The game loads a character from an octane .zip (e.g. assets/characters/finn/finn.zip): <pkg>.bent at the root,
effects/, characters/<car>/<car>.oct (textures embedded), _0.vbuf, _0.ibuf, .mer, .dbl, motions ... The usual
working copy is that archive unpacked next to it (assets/characters/finn/finn/ beside finn.zip), and the toolkit
imports characters/<car>/<car>.oct from there.

pack_export() stages the package - the entries the original archive lists (read from the unpacked folder), the files
of the export in place of their originals - and packs it with offsetting.exe zip-c2 (the community tool that writes
Cars 2 / Toy Story 3 octane zips; never a home-made zip writer: the game's zips are a custom layout). The
.oct.textures folders are left out: they are extracted copies, the .oct carries its textures. A round trip of the
unpacked finn package through this path reproduces every entry of finn.zip byte for byte.
"""
import os
import shutil
import subprocess
import tempfile
import zipfile

import bpy

OFFSETTING = 'offsetting.exe'


def package_root(oct_path):
    """(root, car): the unpacked package folder holding characters/<car>/<car>.oct, or (None, car)."""
    oct_path = os.path.abspath(oct_path)
    car = os.path.splitext(os.path.basename(oct_path))[0]
    d = os.path.dirname(oct_path)
    up = os.path.dirname(d)
    if os.path.basename(up).lower() == 'characters':
        return os.path.dirname(up), car
    return None, car


def original_zip(root):
    """The archive the package folder was unpacked from (<parent>/<folder name>.zip), or None."""
    if not root:
        return None
    z = os.path.join(os.path.dirname(root), os.path.basename(root) + '.zip')
    return z if os.path.isfile(z) else None


def find_offsetting(prefs_path='', assets_dirs=()):
    """offsetting.exe: the add-on preference, else searched in the game's assets folder(s) (two levels deep)."""
    if prefs_path:
        p = bpy.path.abspath(prefs_path)
        if os.path.isdir(p):
            p = os.path.join(p, OFFSETTING)
        return p if os.path.isfile(p) else None
    for a in assets_dirs:
        a = bpy.path.abspath(a or '')
        if not a or not os.path.isdir(a):
            continue
        cands = [os.path.join(a, OFFSETTING)]
        try:
            for e in os.scandir(a):
                if e.is_dir():
                    cands.append(os.path.join(e.path, OFFSETTING))
        except OSError:
            pass
        for c in cands:
            if os.path.isfile(c):
                return c
    return None


def assets_folder_of(path):
    """The game's 'assets' folder above a file, or ''."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.basename(d).lower() == 'assets':
            return d
        up = os.path.dirname(d)
        if up == d:
            return ''
        d = up


def decode_oct(oct_path, offsetting, want_json, want_textures, log):
    """public 2.0.9: the exported .oct also as offsetting.exe's working files, next to it:
         want_json + want_textures  <car>.oct.json referring to <car>.oct.textures/ (decode -t)
         want_json only             <car>.oct.json with the textures inside it (base64)
         want_textures only         <car>.oct.textures/ (decoded -t in a temporary folder, the JSON dropped)
    Older copies of those outputs are replaced. -> list of written paths. Raises RuntimeError."""
    if not (want_json or want_textures):
        return []
    folder = os.path.dirname(os.path.abspath(oct_path))
    name = os.path.basename(oct_path)
    json_out = os.path.join(folder, name + '.json')
    tex_out = os.path.join(folder, name + '.textures')
    work = tempfile.mkdtemp(prefix='c2tk_decode_')
    try:
        src = os.path.join(work, name)
        shutil.copy2(oct_path, src)
        cmd = [offsetting, 'oct', 'decode'] + (['-t'] if want_textures else []) + [src, src + '.json']
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=work,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode != 0 or not os.path.isfile(src + '.json'):
            raise RuntimeError('offsetting.exe oct decode failed: %s' % ((r.stderr or r.stdout or '').strip()[-300:]
                                                                         or 'exit code %d' % r.returncode))
        written = []
        if want_textures:
            if not os.path.isdir(src + '.textures'):
                raise RuntimeError('offsetting.exe oct decode wrote no texture folder')
            if os.path.isdir(tex_out):
                shutil.rmtree(tex_out)          # the export's own folder of an earlier export
            shutil.move(src + '.textures', tex_out)
            written.append(tex_out)
            n = sum(len(fs) for _r, _d, fs in os.walk(tex_out))
            log.append('wrote %s (%d textures)' % (tex_out, n))
        if want_json:
            if os.path.isfile(json_out):
                os.remove(json_out)
            shutil.move(src + '.json', json_out)
            written.append(json_out)
            log.append('wrote %s%s' % (json_out, ' (textures in the folder)' if want_textures
                                       else ' (textures inside, base64)'))
        return written
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _entries(root, zip_path):
    """Archive entry names of the package: the original archive's list, else every file of the unpacked folder
    (without .oct.textures folders, export folders and archives)."""
    if zip_path:
        with zipfile.ZipFile(zip_path) as z:
            return [n for n in z.namelist() if not n.endswith('/')]
    out = []
    for r, ds, fs in os.walk(root):
        ds[:] = [d for d in ds if not d.lower().endswith('.textures') and not d.lower().endswith('export')]
        for f in fs:
            if f.lower().endswith('.zip'):
                continue
            out.append(os.path.relpath(os.path.join(r, f), root).replace('\\', '/'))
    return out


def pack_export(oct_path, export_dir, offsetting, log):
    """Stage and pack the exported character. -> path of the written .zip. Raises RuntimeError with a message
    a user can act on."""
    root, car = package_root(oct_path)
    if root is None:
        raise RuntimeError('the character was not imported from an unpacked game package '
                           '(<package>/characters/%s/%s.oct) - nothing to pack it with' % (car, car))
    zip_path = original_zip(root)
    names = _entries(root, zip_path)
    log.append('package %s (%d entries, list from %s)' % (root, len(names),
                                                          os.path.basename(zip_path) if zip_path else 'the folder'))
    exported = {f.lower(): os.path.join(export_dir, f) for f in os.listdir(export_dir)
                if os.path.isfile(os.path.join(export_dir, f)) and not f.lower().endswith('.zip')}
    own_dir = 'characters/%s/' % car.lower()
    stage = tempfile.mkdtemp(prefix='c2tk_pack_')
    try:
        replaced, missing = [], []
        for n in names:
            base = n.rsplit('/', 1)[-1].lower()
            in_own = n.lower().startswith(own_dir) and n.count('/') == 2
            at_root = '/' not in n
            src = exported.get(base) if (in_own or at_root) else None
            if src is not None:
                replaced.append(n)
            else:
                src = os.path.join(root, n)
                if not os.path.isfile(src):
                    missing.append(n)
                    continue
            dst = os.path.join(stage, n)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        if missing:
            raise RuntimeError('%d file(s) of the package are not in %s, e.g. %s' % (len(missing), root, missing[0]))
        for n in replaced:
            log.append('packed the export\'s ' + n)
        out = os.path.join(export_dir, os.path.basename(root) + '.zip')
        if os.path.exists(out):
            os.remove(out)
        r = subprocess.run([offsetting, 'zip-c2', stage, out], capture_output=True, text=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError('offsetting.exe zip-c2 failed: %s' % ((r.stderr or r.stdout or '').strip()[:300]
                                                                     or 'exit code %d' % r.returncode))
        with zipfile.ZipFile(out) as z:             # reading is fine: check the entries went in
            got = set(z.namelist())
        lost = [n for n in names if n not in got]
        if lost:
            raise RuntimeError('the packed archive is missing %d entries, e.g. %s' % (len(lost), lost[0]))
        log.append('wrote %s (%d entries, %d from the export)' % (out, len(got), len(replaced)))
        return out
    finally:
        shutil.rmtree(stage, ignore_errors=True)
