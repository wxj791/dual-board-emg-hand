"""Crop attributed NinaPro reference photos from the downloaded source figure.

Run manually after placing the unmodified figure at assets/gestures/ninapro/source.png.
No downloading or image generation occurs when the workstation is running.
"""
from pathlib import Path
import hashlib
import json

from PIL import Image

from gesture_config import ASSET_DIR, NINAPRO_ACTIONS


def build():
    directory = ASSET_DIR/'ninapro'
    source = directory/'source.png'
    rows_a = [36, 145, 252, 361, 469, 577, 684, 792, 900, 1008, 1117, 1225, 1333]
    rows_b = [36, 172, 327, 482, 606, 709, 833, 935, 1038, 1162,
              1287, 1411, 1535, 1639, 1742, 1846, 1949, 2073]
    rows_c = [36, 172, 309, 444, 580, 716, 852, 988, 1124, 1260,
              1396, 1532, 1668, 1804, 1940, 2076]
    rows_c2 = [5, 141, 277, 414, 550, 686, 822, 976, 1191]
    manifest = {'source': 'https://ninapro.hevs.ch/figures/SData_Movements.png',
                'paper': 'https://doi.org/10.1038/sdata.2014.53',
                'license': 'CC BY 4.0', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'changes': 'Photo panels cropped from the source figure; no pose manipulation.', 'images': []}
    with Image.open(source) as image:
        if image.size != (1526, 2084):
            raise ValueError('Unexpected source dimensions; crop coordinates require manual review')
        for group, number, key, _, _, _ in NINAPRO_ACTIONS:
            if group == 'A':
                x1, x2, bounds, row = 163, 297, rows_a, number-1
            elif group == 'B':
                x1, x2, bounds, row = 482, 601, rows_b, number-1
            elif number <= 15:
                x1, x2, bounds, row = 786, 904, rows_c, number-1
            else:
                x1, x2, bounds, row = 1090, 1208, rows_c2, number-16
            box = (x1, bounds[row]+3, x2, bounds[row+1]-3)
            image.crop(box).convert('RGB').save(directory/(key+'.png'))
            manifest['images'].append({'key': key, 'reference': f'{group}{number}', 'crop_xyxy': box})
    (directory/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    result = build()
    print(f'Prepared {len(result["images"])} referenced gesture photographs.')
