# coding=UTF-8
"""
Lynxx: frames of a video or an animated picture for the gender model (docs/plans/gender-filter-model.md §3.5, P4:
-gender_verify_media). Used by the model server (POST /v1/frames) and by the model in process. The desktop app core decodes
nothing itself (it is built without Pillow): it only tells videos and animated pictures apart from their first bytes
(file_media_kind) and sends the file to the model server.
- videos (mp4 / mov, webm / mkv, avi): ffmpeg found on PATH (subprocess, time limited), else PyAV when it is installed. The
  container is recognized from its first bytes and forced (-f), only local files may be opened (-protocol_whitelist file):
  a playlist, or a file pointing at other files or URLs, is never followed;
- GIF, APNG, animated WebP: Pillow (ImageSequence), 40 million pixels at most (decompression bombs).
Up to 8 frames evenly spaced, the first and last 5 % left out (title cards, fades). Frames are image bytes (BMP from ffmpeg,
PNG otherwise) of 896 px at most: the model looks at 448 px. Nothing is kept: the caller deletes the media file.
"""
#########################################
#
#

from __future__ import annotations

import io
import json
import pathlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

__all__ = ('MEDIA_MAX_BYTES', 'Frames', 'MediaError', 'decoders', 'extract_frames', 'file_media_kind', 'sniff', 'spread')

MEDIA_MAX_BYTES = 200 * 1024 * 1024
'''largest video / animated picture that is checked (the model server refuses bigger bodies with 413)'''
FRAMES_MAX = 8
EDGE_SKIP = 0.05
'''share of the clip left out at each end'''
FRAME_SIDE_MAX = 896
EXTRACT_TIMEOUT = 120
'''seconds for all the frames of one file'''
TOOL_TIMEOUT = 30
'''seconds for one ffmpeg / ffprobe run'''
PICTURE_PIXELS_MAX = 40_000_000
VIDEO_FORMATS = ('mov', 'matroska', 'avi')
'''ffmpeg demuxer names (mov: mp4 / m4v / mov, matroska: webm / mkv)'''
PICTURE_FORMATS = ('gif', 'png', 'webp')
_MOV_BOXES = (b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip', b'pnot')
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class MediaError(ValueError):
    """this file only: not a video / animated picture, unreadable, or no decoder for it here (unsupported: True)"""
    def __init__(self, text: str, unsupported: bool = False) -> None:
        super().__init__(text)
        self.unsupported = unsupported


@dataclass(slots=True)
class Frames:
    kind: str
    '''video | animated'''
    decoder: str
    '''ffmpeg | pyav | pillow'''
    length: float
    '''duration in seconds (video) or number of frames (animated picture)'''
    frames: list[tuple[float, bytes]] = field(default_factory=list)
    '''(position: seconds or frame number, image bytes)'''


def sniff(head: bytes) -> str:
    """container / picture format from the first bytes: gif, png, webp, mov, matroska, avi; '' when unknown"""
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    if head[:4] == b'\x1aE\xdf\xa3':
        return 'matroska'
    if head[4:8] in _MOV_BOXES:
        return 'mov'
    return ''


def spread(n: int) -> list[float]:
    """n positions in a clip (0..1), evenly spaced, the first and last 5 % left out"""
    return [EDGE_SKIP + (k + 0.5) * (1 - 2 * EDGE_SKIP) / n for k in range(max(0, n))]


# ---------------- what a downloaded file is (pure Python: the app core has no decoder) ----------------

def _skip_sub_blocks(f) -> None:
    while (size := f.read(1)) and size[0]:
        f.seek(size[0], 1)


def _gif_frames(f, stop: int) -> int:
    """number of frames of a GIF, counted up to stop (block walk, no decoding)"""
    f.seek(10)
    packed = f.read(1)
    if not packed:
        return 0
    f.seek(13)
    if packed[0] & 0x80:  # global color table
        f.seek(3 * (2 << (packed[0] & 7)), 1)
    n = 0
    while (b := f.read(1)) and b != b'\x3b':
        if b == b'\x21':  # extension: label, sub-blocks
            f.read(1)
            _skip_sub_blocks(f)
        elif b == b'\x2c':  # image descriptor: 9 bytes, local color table, LZW code size, sub-blocks
            d = f.read(9)
            if len(d) < 9:
                break
            n += 1
            if n >= stop:
                break
            if d[8] & 0x80:
                f.seek(3 * (2 << (d[8] & 7)), 1)
            f.read(1)
            _skip_sub_blocks(f)
        else:
            break
    return n


def _apng_frames(f) -> int:
    """frames announced by the acTL chunk of a PNG (0: a still PNG), chunks read up to the first IDAT"""
    f.seek(8)
    for _ in range(1000):
        hdr = f.read(8)
        if len(hdr) < 8:
            return 0
        size, kind = int.from_bytes(hdr[:4], 'big'), hdr[4:]
        if kind == b'acTL':
            data = f.read(4)
            return int.from_bytes(data, 'big') if len(data) == 4 else 0
        if kind in (b'IDAT', b'IEND'):
            return 0
        f.seek(size + 4, 1)  # data + crc
    return 0


def file_media_kind(path: pathlib.Path) -> str:
    """'video', 'animated' (GIF / APNG / WebP with more than one frame) or '' (a still picture, audio, anything else)"""
    with open(path, 'rb') as f:
        head = f.read(32)
        fmt = sniff(head)
        if fmt in VIDEO_FORMATS:
            return 'video'
        if fmt == 'gif':
            return 'animated' if _gif_frames(f, 2) >= 2 else ''
        if fmt == 'png':
            return 'animated' if _apng_frames(f) >= 2 else ''
        if fmt == 'webp':  # VP8X chunk, animation flag
            return 'animated' if head[12:16] == b'VP8X' and len(head) > 20 and head[20] & 0x02 else ''
    return ''


# ---------------- decoders ----------------

def _tool(name: str) -> str:
    return shutil.which(name) or ''


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=max(1.0, timeout), stdin=subprocess.DEVNULL, check=False,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise MediaError(f'{pathlib.Path(args[0]).stem} took longer than {max(1.0, timeout):.0f} s') from None
    except OSError as err:
        raise MediaError(f'{pathlib.Path(args[0]).stem} could not run ({err.strerror or err})', True) from None


_decoders: dict[str, str] | None = None
_decoders_lock = threading.Lock()


def decoders(refresh: bool = False) -> dict[str, str]:
    """{'video': 'ffmpeg 6.1.1' | 'pyav 14.0.1' | '', 'animated': 'pillow 11.0.0' | ''}: what this process can read"""
    global _decoders
    with _decoders_lock:
        if _decoders is None or refresh:
            video = ''
            if ffmpeg := _tool('ffmpeg'):
                try:
                    out = _run([ffmpeg, '-hide_banner', '-version'], 10)
                    m = re.match(rb'ffmpeg version (\S+)', out.stdout)
                    video = f'ffmpeg {m.group(1).decode(errors="replace")[:40]}' if out.returncode == 0 and m else ''
                except MediaError:
                    video = ''
            if not video:
                try:
                    import av
                    video = f'pyav {getattr(av, "__version__", "?")}'
                except ImportError:
                    pass
            try:
                import PIL
                animated = f'pillow {PIL.__version__}'
            except ImportError:
                animated = ''
            _decoders = {'video': video, 'animated': animated}
        return dict(_decoders)


def extract_frames(path: pathlib.Path, *, timeout: float = EXTRACT_TIMEOUT, max_frames: int = FRAMES_MAX) -> Frames:
    """Up to max_frames frames of the video / animated picture at path. MediaError: this file only"""
    deadline = time.monotonic() + timeout
    with open(path, 'rb') as f:
        fmt = sniff(f.read(32))
    if fmt in PICTURE_FORMATS:
        return _picture_frames(path, fmt, deadline, max_frames)
    if fmt in VIDEO_FORMATS:
        if ffmpeg := _tool('ffmpeg'):
            return _ffmpeg_frames(ffmpeg, path, fmt, deadline, max_frames)
        try:
            import av  # noqa: F401
        except ImportError:
            raise MediaError('no video decoder here: install ffmpeg (on PATH) or PyAV', True) from None
        return _pyav_frames(path, fmt, deadline, max_frames)
    raise MediaError('not a video (mp4, webm, mkv, mov, avi) or an animated picture (gif, png, webp)', True)


def _png_bytes(im) -> bytes:
    im.thumbnail((FRAME_SIDE_MAX, FRAME_SIDE_MAX))
    buf = io.BytesIO()
    im.save(buf, 'PNG', compress_level=1)
    return buf.getvalue()


def _picture_frames(path: pathlib.Path, fmt: str, deadline: float, max_frames: int) -> Frames:
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        raise MediaError('animated pictures need Pillow (pip install pillow)', True) from None
    Image.MAX_IMAGE_PIXELS = PICTURE_PIXELS_MAX // 2  # Pillow refuses twice this size
    try:
        with Image.open(path, formats=[fmt.upper()]) as im:
            count = max(1, int(getattr(im, 'n_frames', 1)))
            picks = sorted({min(count - 1, int(count * p)) for p in spread(min(max_frames, count))})
            seq = ImageSequence.Iterator(im)
            out = Frames('animated', 'pillow', float(count))
            for i in picks:
                if time.monotonic() > deadline:
                    raise MediaError('reading the frames took too long')
                out.frames.append((float(i), _png_bytes(seq[i].convert('RGBA'))))
            return out
    except MediaError:
        raise
    except Exception as err:  # truncated file, decompression bomb, unknown frame data
        raise MediaError(f'unreadable {fmt} ({type(err).__name__})') from None


def _video_info(ffmpeg: str, path: pathlib.Path, fmt: str, deadline: float) -> tuple[float, int]:
    """(duration in seconds, number of frames or 0 when unknown)"""
    src = f'file:{path}'
    if ffprobe := _tool('ffprobe'):
        out = _run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-f', fmt, '-select_streams', 'v:0',
                    '-show_entries', 'format=duration:stream=duration,nb_frames', '-of', 'json', src],
                   min(TOOL_TIMEOUT, deadline - time.monotonic()))
        try:
            info = json.loads(out.stdout or b'{}')
        except ValueError:
            info = {}
        streams = info.get('streams') or []
        if out.returncode != 0 or not streams:
            raise MediaError('no video stream could be read' if out.returncode == 0 else 'unreadable video')

        def num(v, kind):
            try:
                return kind(v)
            except (TypeError, ValueError):
                return kind(0)
        duration = num(streams[0].get('duration'), float) or num((info.get('format') or {}).get('duration'), float)
        return duration, num(streams[0].get('nb_frames'), int)
    # no ffprobe: 'Duration: 00:00:05.00' in what ffmpeg says about its input
    out = _run([ffmpeg, '-hide_banner', '-nostdin', '-protocol_whitelist', 'file', '-f', fmt, '-i', src],
               min(TOOL_TIMEOUT, deadline - time.monotonic()))
    text = out.stderr.decode(errors='replace')
    if not re.search(r'Stream #\S+.*Video:', text):
        raise MediaError('no video stream could be read')
    m = re.search(r'Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)', text)
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0, 0


def _ffmpeg_frames(ffmpeg: str, path: pathlib.Path, fmt: str, deadline: float, max_frames: int) -> Frames:
    duration, count = _video_info(ffmpeg, path, fmt, deadline)
    if duration <= 0:
        raise MediaError('the video duration is unknown')
    n = min(max_frames, count) if count > 0 else max_frames
    scale = f"scale=w='min(iw,{FRAME_SIDE_MAX})':h='min(ih,{FRAME_SIDE_MAX})':force_original_aspect_ratio=decrease"
    out = Frames('video', 'ffmpeg', round(duration, 3))
    last_error = ''
    for p in spread(n):
        left = deadline - time.monotonic()
        if left <= 1:
            raise MediaError('reading the frames took too long')
        t = duration * p
        r = _run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-protocol_whitelist', 'file', '-f', fmt,
                  '-ss', f'{t:.3f}', '-i', f'file:{path}', '-map', '0:v:0', '-frames:v', '1', '-an', '-sn', '-dn',
                  '-vf', scale, '-f', 'image2pipe', '-c:v', 'bmp', 'pipe:1'], min(TOOL_TIMEOUT, left))
        if r.returncode == 0 and r.stdout[:2] == b'BM':
            out.frames.append((round(t, 3), r.stdout))
        else:
            last_error = r.stderr.decode(errors='replace').strip().splitlines()[-1:] or ['no frame']
            last_error = last_error[0][:160]
    if not out.frames:
        raise MediaError(f'no frame could be read ({last_error or "ffmpeg"})')
    return out


def _pyav_frames(path: pathlib.Path, fmt: str, deadline: float, max_frames: int) -> Frames:
    import av
    try:
        with av.open(str(path), format=fmt) as c:
            stream = next((s for s in c.streams if s.type == 'video'), None)
            if stream is None:
                raise MediaError('no video stream could be read')
            if stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            else:
                duration = float(c.duration or 0) / 1_000_000  # av.time_base
            if duration <= 0:
                raise MediaError('the video duration is unknown')
            count = int(stream.frames or 0)
            out = Frames('video', 'pyav', round(duration, 3))
            for p in spread(min(max_frames, count) if count > 0 else max_frames):
                if time.monotonic() > deadline:
                    raise MediaError('reading the frames took too long')
                t = duration * p
                c.seek(int(t / stream.time_base), stream=stream, backward=True, any_frame=False)  # key frame at or before t
                for frame in c.decode(stream):
                    if frame.time is None or frame.time >= t - 0.001:
                        out.frames.append((round(t, 3), _png_bytes(frame.to_image())))
                        break
            if not out.frames:
                raise MediaError('no frame could be read')
            return out
    except MediaError:
        raise
    except Exception as err:  # av.error.*: broken file, unknown codec
        raise MediaError(f'unreadable video ({type(err).__name__})') from None


def main(argv: list[str] | None = None) -> int:
    """python -m lynxx.media_frames FILE...: the frames a check would look at (decoder, positions, sizes)"""
    import sys
    args = sys.argv[1:] if argv is None else argv
    print(json.dumps({'decoders': decoders()}))
    for name in args:
        try:
            p = pathlib.Path(name)
            fr = extract_frames(p)
            print(json.dumps({'file': name, 'kind': file_media_kind(p), 'decoder': fr.decoder, 'length': fr.length,
                              'frames': [{'at': at, 'bytes': len(data)} for at, data in fr.frames]}))
        except (OSError, MediaError) as err:
            print(json.dumps({'file': name, 'error': str(err)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#
#
#########################################
