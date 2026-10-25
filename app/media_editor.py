"""Bounded, local FFmpeg edits. Request values never become shell expressions."""
import math
from pathlib import Path
import re
import subprocess

CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Informe um valor válido para ' + label + '.')
    return float(value)


def validate_edit(body, duration):
    sections = body.get('sections')
    if sections is None:
        sections = [body['section']] if body.get('section') else []
    if not isinstance(sections, list) or not 1 <= len(sections) <= 24:
        raise ValueError('Selecione de 1 a 24 trechos para editar.')
    duration = number(duration, 'a duração do vídeo')
    normalized = []
    for item in sections:
        if not isinstance(item, dict) or set(item) != {'start', 'end'}:
            raise ValueError('Cada trecho precisa de início e fim.')
        start, end = number(item['start'], 'o início'), number(item['end'], 'o fim')
        if start < 0 or end <= start or end > duration + .01 or end - start < .1 - 1e-9:
            raise ValueError('Escolha trechos de pelo menos 0,1 segundo dentro da duração do vídeo.')
        normalized.append({'start': round(start, 3), 'end': round(end, 3)})
    total = sum(item['end'] - item['start'] for item in normalized)
    mode = body.get('mode', 'video')
    if mode not in ('video', 'audio', 'gif'):
        raise ValueError('Escolha vídeo, áudio ou GIF.')
    if mode == 'gif' and total > 30:
        raise ValueError('Para criar um GIF, escolha no máximo 30 segundos no total.')
    if total > 7200:
        raise ValueError('A montagem pode ter no máximo duas horas. Baixe trechos maiores separadamente.')
    vertical = body.get('vertical', False)
    if type(vertical) is not bool or (vertical and mode != 'video'):
        raise ValueError('O enquadramento vertical está disponível para vídeo MP4.')
    crop = body.get('crop', {'x': .5, 'y': .5})
    if not isinstance(crop, dict) or set(crop) != {'x', 'y'}:
        raise ValueError('Escolha a posição do enquadramento na prévia.')
    crop = {key: number(crop[key], 'o enquadramento') for key in ('x', 'y')}
    if any(not 0 <= value <= 1 for value in crop.values()):
        raise ValueError('O enquadramento precisa ficar dentro da imagem.')
    return {'sections': normalized, 'mode': mode, 'vertical': vertical, 'crop': crop,
            'duration': round(total, 3)}


def probe_media(ffmpeg, path, env=None):
    result = subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-i', str(path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=30, creationflags=CREATE_FLAGS, env=env)
    text = result.stderr
    duration = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
    video = next((line for line in text.splitlines() if 'Video:' in line), '')
    dimensions = re.search(r'\b(\d{2,5})x(\d{2,5})\b', video)
    fps = re.search(r'([\d.]+) fps', video)
    if not duration:
        raise ValueError('Não foi possível conferir o arquivo da edição. Tente baixar novamente.')
    hours, minutes, seconds = map(float, duration.groups())
    return {'duration': hours * 3600 + minutes * 60 + seconds,
            'width': int(dimensions[1]) if dimensions else None,
            'height': int(dimensions[2]) if dimensions else None,
            'fps': float(fps[1]) if fps else 30, 'audio': 'Audio:' in text}


def frame_aligned_edit(edit, info, offset=0):
    """Account for complete source frames before joining every selected segment.

    The requested edit remains unchanged. This effective copy makes frame
    rounding visible instead of silently truncating the end of a long montage.
    Audio-only edits retain their exact sample-based time selections.
    """
    if edit.get('_frame_aligned') or len(edit['sections']) == 1 or edit['mode'] == 'audio':
        return edit, None
    fps = info.get('fps') or 30
    for rate in (24000 / 1001, 30000 / 1001, 60000 / 1001):
        if abs(fps - rate) < .005:
            fps = rate
            break
    ranges, sections = [], []
    changed = False
    available = max(1, int(math.ceil(info['duration'] * fps - .05)))
    for section in edit['sections']:
        first = max(0, int(math.floor((section['start'] - offset) * fps + .5)))
        last = min(available, int(math.floor((section['end'] - offset) * fps + .5)))
        if last <= first:
            raise ValueError('Um trecho ficou menor que um quadro. Aumente sua duração.')
        start, end = offset + first / fps, offset + last / fps
        changed = changed or abs(start - section['start']) > .001 or abs(end - section['end']) > .001
        ranges.append((first, last))
        sections.append({'start': start, 'end': end})
    result = dict(edit, sections=sections, duration=sum((last-first)/fps for first,last in ranges),
                  _frame_aligned=True, _frame_ranges=ranges)
    if edit['mode'] == 'gif' and result['duration'] > 30 + 1e-9:
        raise ValueError('Após ajustar aos quadros, o GIF passa de 30 segundos. Reduza um dos trechos.')
    note = ('Os limites dos trechos foram ajustados aos quadros do arquivo baixado; '
            'a duração final é ' + f"{result['duration']:.3f}".replace('.', ',') + ' s.') if changed else None
    return result, note


def transform_command(ffmpeg, source, target, edit, info, offset=0):
    """Build one decode/concat operation, preserving the user's section order."""
    edit, alignment_note = frame_aligned_edit(edit, info, offset)
    filters, inputs = [], []
    audio_only = edit['mode'] == 'audio'
    has_audio = info['audio'] and edit['mode'] != 'gif'
    vertical = edit['vertical']
    note = alignment_note
    if vertical:
        width, height = info.get('width'), info.get('height')
        if not width or not height:
            raise ValueError('Não foi possível conferir as dimensões para converter em 9:16.')
        if abs(width / height - 9 / 16) < .015:
            vertical = False
            note = (note + ' ' if note else '') + 'O vídeo já está em 9:16. O corte foi salvo sem reenquadrar.'
    for index, section in enumerate(edit['sections']):
        start, end = max(0, section['start'] - offset), section['end'] - offset
        if not audio_only:
            bounds = edit.get('_frame_ranges')
            trim = (f'trim=start_frame={bounds[index][0]}:end_frame={bounds[index][1]}' if bounds
                    else f'trim=start={start:.3f}:end={end:.3f}')
            filters.append(f'[0:v:0]{trim},setpts=PTS-STARTPTS[v{index}]')
            inputs.append(f'[v{index}]')
        if has_audio:
            filters.append(f'[0:a:0]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS[a{index}]')
            inputs.append(f'[a{index}]')
    if audio_only and not has_audio:
        raise ValueError('Este vídeo não contém uma faixa de áudio para exportar.')
    count = len(edit['sections'])
    outputs = ('[aout]' if audio_only else '[vjoin][aout]' if has_audio else '[vjoin]')
    filters.append(''.join(inputs) + f'concat=n={count}:v={0 if audio_only else 1}:a={1 if has_audio else 0}' + outputs)
    if not audio_only:
        tail = []
        if vertical:
            x, y = edit['crop']['x'], edit['crop']['y']
            tail.append("crop=w='trunc(min(iw,ih*9/16)/18)*18':h='trunc(min(ih,iw*16/9)/32)*32':"
                        + f"x='(iw-ow)*{x:.6f}':y='(ih-oh)*{y:.6f}'")
        if edit['mode'] == 'gif':
            tail.extend(['fps=12', "scale=w='min(640,iw)':h=-1:flags=lanczos", 'split[gp][gi]'])
            filters.append('[vjoin]' + ','.join(tail))
            filters.extend(['[gp]palettegen=stats_mode=diff[palette]', '[gi][palette]paletteuse=dither=sierra2_4a[vout]'])
        else:
            tail.extend(['scale=trunc(iw/2)*2:trunc(ih/2)*2', 'setsar=1', 'format=yuv420p'])
            filters.append('[vjoin]' + ','.join(tail) + '[vout]')
    command = [ffmpeg, '-hide_banner', '-nostdin', '-y', '-i', str(source),
               '-filter_complex_threads', '2', '-filter_complex', ';'.join(filters)]
    if audio_only:
        command += ['-map', '[aout]', '-c:a', 'libmp3lame', '-q:a', '2']
    elif edit['mode'] == 'gif':
        command += ['-map', '[vout]', '-an', '-loop', '0']
    else:
        command += ['-map', '[vout]']
        if has_audio:
            command += ['-map', '[aout]', '-c:a', 'aac', '-b:a', '160k']
        command += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2', '-movflags', '+faststart']
    return command + [str(target)], note


def final_suffix(mode):
    return '.gif' if mode == 'gif' else '.mp3' if mode == 'audio' else '.mp4'
