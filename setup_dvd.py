#!/usr/bin/env python3
"""Build the optional local DVD reader from pinned official FFmpeg sources.

macOS prerequisites: Apple Command Line Tools; brew install libdvdnav libdvdread pkg-config
The normal FFmpeg installation is not changed. Re-run install.py after this script.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

VERSION = '8.1.2'
SHA256 = '464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c'


def fix_dvd_discontinuities(source):
    """Avoid unsigned 32-bit wrap when a DVD cut skips forward in source PTM.

    Both PTM fields are uint32_t. Subtraction must be signed before accumulating
    into the int64_t timeline offset, otherwise a skip adds ~13 hours.
    """
    path = source/'libavformat/dvdvideodec.c'
    text = path.read_text()
    old = 'state->ptm_offset  += state->vobu_e_ptm - '
    new = 'state->ptm_offset  += (int64_t)state->vobu_e_ptm - '
    if text.count(old) == 2:
        path.write_text(text.replace(old,new))
    elif text.count(new) != 2:
        raise RuntimeError('Unexpected FFmpeg DVD timing implementation; refusing an unverified patch.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Reuse the unpacked FFmpeg source/build directory')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    target = root/'codex_subs/bin/dvd-subtitles'
    subprocess.run(['pkg-config','--exists','dvdnav','dvdread'], check=True)
    with tempfile.TemporaryDirectory(prefix='codex-dvd-build-') as temporary:
        temp = Path(temporary)
        source = args.source.resolve() if args.source else temp/f'ffmpeg-{VERSION}'
        if not args.source:
            archive = temp/'ffmpeg.tar.xz'
            print('Downloading official FFmpeg sources...', flush=True)
            urllib.request.urlretrieve(f'https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz', archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
                raise RuntimeError('FFmpeg source checksum did not match.')
            with tarfile.open(archive) as bundle:
                bundle.extractall(temp, filter='data')
            subprocess.run(['./configure','--disable-everything','--disable-programs','--disable-doc',
                            '--disable-network','--disable-autodetect','--disable-asm','--enable-gpl',
                            '--enable-libdvdnav','--enable-libdvdread','--enable-demuxer=dvdvideo,mpegvideo',
                            '--enable-decoder=dvdsub','--enable-parser=dvdsub,mpegvideo,ac3',
                            '--enable-protocol=file','--disable-avfilter','--disable-avdevice',
                            '--disable-swresample','--disable-swscale'], cwd=source, check=True)
        fix_dvd_discontinuities(source)
        subprocess.run(['make',f'-j{min(os.cpu_count() or 2,6)}','libavformat/libavformat.a',
                        'libavcodec/libavcodec.a','libavutil/libavutil.a','libavformat/avlanguage.o'],
                       cwd=source, check=True)
        flags = shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs','dvdnav','dvdread'], text=True))
        binary = temp/'dvd-subtitles'
        subprocess.run(['cc','-O2','-I',str(source),str(root/'native/dvd_subtitles.c'),
                        str(source/'libavformat/avlanguage.o'), # dvdvideo minimal build needs this language table.
                        str(source/'libavformat/libavformat.a'),str(source/'libavcodec/libavcodec.a'),
                        str(source/'libavutil/libavutil.a'),*flags,
                        '-framework','CoreFoundation','-framework','CoreVideo','-framework','CoreMedia',
                        '-framework','VideoToolbox','-framework','AudioToolbox','-lm','-lz','-lbz2','-liconv',
                        '-o',str(binary)], check=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        shutil.copy2(source/'COPYING.GPLv2', target.parent/'COPYING.FFmpeg.GPLv2')
        (target.parent/'FFmpeg-SOURCE.txt').write_text(
            f'FFmpeg {VERSION}, upstream source: https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz\n'
            f'SHA256: {SHA256}\nLocal change: cast DVD PTM discontinuity subtraction to int64_t (two sites).\n'
            'Exact patch and build configuration: setup_dvd.py; application code: native/dvd_subtitles.c\n')
    print(f'Built {target}. Run python3 install.py to update VLC.')


if __name__ == '__main__':
    main()
