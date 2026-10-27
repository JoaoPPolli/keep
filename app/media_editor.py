"""Bounded, local FFmpeg edits. Request values never become shell expressions."""
import math
import os
import shutil
from pathlib import Path
import re
import subprocess

CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Informe um valor válido para ' + label + '.')
    return float(value)


def normalized_rectangle(value, label):
    """Validate a source-image selection independently of its pixel dimensions."""
    if not isinstance(value, dict) or set(value) != {'x', 'y', 'width', 'height'}:
        raise ValueError('Escolha uma área válida para ' + label + '.')
    rectangle = {key: number(value[key], 'a área de ' + label) for key in value}
    x, y, width, height = (rectangle[key] for key in ('x', 'y', 'width', 'height'))
    if (x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1 + 1e-9 or y + height > 1 + 1e-9):
        raise ValueError('A área de ' + label + ' precisa ficar dentro da imagem.')
    return rectangle


MAX_CAPTION_CUES = 4000
MAX_CAPTION_CHARACTERS = 200000


def validate_captions(value, duration):
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('Escolha se deseja incluir as legendas.')
    if not value['enabled']:
        return None
    cues = value.get('cues')
    if not isinstance(cues, list) or not 1 <= len(cues) <= MAX_CAPTION_CUES:
        raise ValueError('Gere e confira as legendas antes de baixar.')
    result, previous, total = [], 0, 0
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) != {'start', 'end', 'text'}:
            raise ValueError('Uma legenda está inválida. Gere as legendas novamente.')
        start, end = number(cue['start'], 'o início da legenda'), number(cue['end'], 'o fim da legenda')
        text = cue['text']
        if (not isinstance(text, str) or len(text) > 180 or any(ord(c) < 32 and not c.isspace() for c in text)
                or not 0 <= start < end <= duration + .01 or start < previous - .001):
            raise ValueError('Confira os textos e horários das legendas; eles devem estar dentro do vídeo e sem sobreposição.')
        text = ' '.join(text.split())
        if not text:
            raise ValueError('Uma legenda está vazia. Corrija o texto antes de baixar.')
        total += len(text)
        if total > MAX_CAPTION_CHARACTERS:
            raise ValueError('Há texto demais nas legendas. Escolha um trecho menor.')
        rounded_start, rounded_end = round(start, 3), round(end, 3)
        if rounded_end <= rounded_start:
            raise ValueError('Uma legenda é curta demais. Aumente seu intervalo para pelo menos 0,001 segundo.')
        result.append({'start': rounded_start, 'end': rounded_end, 'text': text})
        previous = end
    return {'enabled': True, 'cues': result}


def wrap_caption_text(text):
    lines, line = [], ''
    for word in text.split():
        candidate = line + (' ' if line else '') + word
        if line and len(candidate) > 32:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > 2:
        lines = [lines[0], ' '.join(lines[1:])]
    return lines


def caption_timeline(edit):
    """Map source-clock captions through cuts, reordered sections and joins."""
    cues = edit.get('captions', {}).get('cues', [])
    result, elapsed = [], 0.0
    for section in edit['sections']:
        for cue in cues:
            start, end = max(cue['start'], section['start']), min(cue['end'], section['end'])
            if end > start:
                result.append({'start': elapsed + start - section['start'],
                               'end': elapsed + end - section['start'], 'text': cue['text']})
        elapsed += section['end'] - section['start']
    return result


def caption_filter(edit, width, directory):
    """Use bounded sidecar files, so text never becomes a filter expression.

    The caller runs FFmpeg with directory as cwd. sendcmd keeps the argv small
    even for thousands of captions, including on embedded Windows Python.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    windows = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    candidates = [windows / 'arialbd.ttf',
        Path('/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf'),
        Path('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')]
    font = next((path for path in candidates if path.is_file()), None)
    font_option = ''
    if font:
        shutil.copyfile(font, directory / 'caption-font.ttf')
        font_option = 'fontfile=caption-font.ttf:'
    base = width * .052
    events = []
    # sendcmd uses av_get_token quoting. Single-quoted text is opaque; an
    # apostrophe is emitted outside the quote with a backslash escape. Runtime
    # `text` updates one option directly, without reinit's option-string parser.
    quote_text = lambda text: "'" + text.replace("'", "'\\''") + "'"
    for cue in caption_timeline(edit):
        lines = wrap_caption_text(cue['text'])
        font_size = max(1, base * min(1, 32 / max(map(len, lines))))
        at = f"{cue['start']:.6f} drawtext@keep "
        commands = [at + f'fontsize {font_size:.6f};',
                    at + f'borderw {font_size * .09:.6f};',
                    at + f'line_spacing {round(font_size * .2)};',
                    at + 'text ' + quote_text('\n'.join(lines)) + ';']
        events.append((cue['start'], 1, '\n'.join(commands)))
        events.append((cue['end'], 0, f"{cue['end']:.6f} drawtext@keep text ' ';"))
    (directory / 'caption-commands.txt').write_text('\n'.join(event[2] for event in sorted(events)), encoding='utf-8')
    return ('sendcmd=f=caption-commands.txt,drawtext@keep=' + font_option
        + f'text= :expansion=none:fontsize={base:.6f}:line_spacing={round(base * .2)}:'
        + f'fontcolor=white:bordercolor=black:borderw={base * .09:.6f}:x=(w-text_w)/2:y=h*0.92-text_h')



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
    layout = body.get('layout', 'full')
    if layout not in ('full', 'stacked'):
        raise ValueError('Escolha o modo Cheia ou Empilhado.')
    result = {'sections': normalized, 'mode': mode, 'vertical': vertical, 'crop': crop,
              'duration': round(total, 3), 'layout': layout}
    if layout == 'stacked':
        if mode != 'video' or not vertical:
            raise ValueError('O modo Empilhado está disponível para vídeo vertical MP4.')
        result['camera'] = normalized_rectangle(body.get('camera', {
            'x': 0, 'y': 0, 'width': .28, 'height': .28}), 'a câmera')
        result['content'] = normalized_rectangle(body.get('content', {
            'x': .25, 'y': 0, 'width': .5, 'height': 1}), 'o conteúdo')
        share = number(body.get('camera_height', .3), 'a altura da câmera')
        if not .15 <= share <= .6:
            raise ValueError('A câmera deve ocupar entre 15% e 60% da altura vertical.')
        result['camera_height'] = share
    captions = validate_captions(body.get('captions'), duration)
    if captions:
        if mode != 'video':
            raise ValueError('As legendas estão disponíveis para vídeo MP4.')
        result['captions'] = captions
    return result


def stacked_geometry(edit, info):
    """Return the exact even-pixel selections used by both panes of the export.

    The canvas preview mirrors these calculations. Source coordinates are
    quantized before a centered cover crop, so no arbitrary FFmpeg expression
    or coordinate reaches the filter graph.
    """
    width, height = info.get('width'), info.get('height')
    if (isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, (int, float)) or not isinstance(height, (int, float))
            or not math.isfinite(width) or not math.isfinite(height)
            or width < 18 or height < 2):
        raise ValueError('Não foi possível conferir as dimensões para montar o vídeo vertical.')
    width, height = int(width), int(height)
    even = lambda value: 2 * math.floor(value / 2)
    units = math.floor(min(width, 1080) / 18)
    output_width, output_height = units * 18, units * 32
    share = number(edit.get('camera_height', .3), 'a altura da câmera')
    if not .15 <= share <= .6:
        raise ValueError('A câmera deve ocupar entre 15% e 60% da altura vertical.')
    camera_height = even(output_height * share)
    content_height = output_height - camera_height

    def selection(value, label, pane_height):
        rectangle = normalized_rectangle(value, label)
        left, top = even(rectangle['x'] * width), even(rectangle['y'] * height)
        region_width = min(even(rectangle['width'] * width), even(width - left))
        region_height = min(even(rectangle['height'] * height), even(height - top))
        if min(region_width, region_height) < 2:
            raise ValueError('A área de ' + label + ' é pequena demais. Aumente a seleção.')
        if region_width / region_height > output_width / pane_height:
            crop_width, crop_height = even(region_height * output_width / pane_height), region_height
        else:
            crop_width, crop_height = region_width, even(region_width * pane_height / output_width)
        if min(crop_width, crop_height) < 2:
            raise ValueError('A área de ' + label + ' é estreita demais. Aumente a seleção.')
        return {'x': left + even((region_width - crop_width) / 2),
                'y': top + even((region_height - crop_height) / 2),
                'width': crop_width, 'height': crop_height}

    return {'width': output_width, 'height': output_height,
            'camera_height': camera_height, 'content_height': content_height,
            'camera': selection(edit['camera'], 'a câmera', camera_height),
            'content': selection(edit['content'], 'o conteúdo', content_height)}


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
                  _frame_aligned=True, _frame_ranges=ranges, _frame_rate=fps)
    if edit['mode'] == 'gif' and result['duration'] > 30 + 1e-9:
        raise ValueError('Após ajustar aos quadros, o GIF passa de 30 segundos. Reduza um dos trechos.')
    note = ('Os limites dos trechos foram ajustados aos quadros do arquivo baixado; '
            'a duração final é ' + f"{result['duration']:.3f}".replace('.', ',') + ' s.') if changed else None
    return result, note


def transform_command(ffmpeg, source, target, edit, info, offset=0, caption_dir=None):
    """Build one decode/concat operation, preserving the user's section order."""
    edit, alignment_note = frame_aligned_edit(edit, info, offset)
    filters, inputs = [], []
    audio_only = edit['mode'] == 'audio'
    has_audio = info['audio'] and edit['mode'] != 'gif'
    vertical = edit['vertical']
    stacked = edit.get('layout', 'full') == 'stacked'
    if stacked and (not vertical or edit['mode'] != 'video'):
        raise ValueError('O modo Empilhado está disponível para vídeo vertical MP4.')
    note = alignment_note
    if vertical:
        width, height = info.get('width'), info.get('height')
        if not width or not height:
            raise ValueError('Não foi possível conferir as dimensões para converter em 9:16.')
        if not stacked and abs(width / height - 9 / 16) < .015:
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
        video_label = 'vjoin'
        if stacked:
            geometry = stacked_geometry(edit, info)
            filters.append('[vjoin]split=2[camera_in][content_in]')
            for pane in ('camera', 'content'):
                rectangle = geometry[pane]
                pane_height = geometry[pane + '_height']
                filters.append(f'[{pane}_in]crop={rectangle["width"]}:{rectangle["height"]}:'
                               f'{rectangle["x"]}:{rectangle["y"]},'
                               f'scale={geometry["width"]}:{pane_height}:flags=lanczos,'
                               f'setsar=1[{pane}_out]')
            filters.append('[camera_out][content_out]vstack=inputs=2[vstack]')
            video_label = 'vstack'
        tail = []
        if edit.get('_frame_ranges'):
            # concat's video-only segments can omit the last frame duration.
            # Frame-aligned cuts already use a constant frame clock; retain
            # every selected frame and rebuild that clock after joining.
            tail.append(f'setpts=N/({edit['_frame_rate']:.9f}*TB)')
        if vertical and not stacked:
            x, y = edit['crop']['x'], edit['crop']['y']
            tail.append("crop=w='trunc(min(iw,ih*9/16)/18)*18':h='trunc(min(ih,iw*16/9)/32)*32':"
                        + f"x='(iw-ow)*{x:.6f}':y='(ih-oh)*{y:.6f}'")
        if edit['mode'] == 'gif':
            tail.extend(['fps=12', "scale=w='min(640,iw)':h=-1:flags=lanczos", 'split[gp][gi]'])
            filters.append('[' + video_label + ']' + ','.join(tail))
            filters.extend(['[gp]palettegen=stats_mode=diff[palette]', '[gi][palette]paletteuse=dither=sierra2_4a[vout]'])
        else:
            if edit.get('captions'):
                if caption_dir is None:
                    raise ValueError('Não foi possível preparar as legendas da exportação.')
                if stacked:
                    output_width = geometry['width']
                elif vertical:
                    output_width = math.floor(min(info['width'], info['height'] * 9 / 16) / 18) * 18
                else:
                    output_width = 2 * math.floor(info['width'] / 2)
                # No intersecting cues means this selected section has no speech.
                if caption_timeline(edit):
                    tail.append(caption_filter(edit, output_width, caption_dir))
            tail.extend(['scale=trunc(iw/2)*2:trunc(ih/2)*2', 'setsar=1', 'format=yuv420p'])
            filters.append('[' + video_label + ']' + ','.join(tail) + '[vout]')
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
        if edit.get('_frame_ranges'):
            command += ['-r', f"{edit['_frame_rate']:.9f}", '-fps_mode', 'cfr']
        command += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2', '-movflags', '+faststart']
    return command + [str(target)], note


def final_suffix(mode):
    return '.gif' if mode == 'gif' else '.mp3' if mode == 'audio' else '.mp4'
