"""Portable gesture vocabulary and logical hand-action mappings.

Every recording owns an immutable JSON snapshot. Class zero is always rest;
class IDs are local to a profile, never global finger/servo command numbers.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent/'assets/gestures'
PROFILE_FILE = Path(__file__).resolve().parent/'settings/gesture_profile.json'
MAX_ACTIONS = 15  # PC vocabulary bound, not a claim about deployed FPGA capacity.

ACTION_LIBRARY = {
    'rest': ('静息', '手和前臂自然放松，不要发力。', 0),
    'fist': ('握拳', '五指握拳，中等力度，保持每次发力一致。', 1),
    'open': ('张开手掌', '五指自然伸直张开，保持稳定。', 2),
    'thumbs_up': ('点赞', '大拇指竖起，其余四指握住。', 5),
    'scissors': ('剪刀手', '食指和中指伸出成V字，其余手指握住。', 6),
    'one': ('数字一', '只伸出食指，其余四指握住。', 7),
    'six': ('数字六', '拇指和小指伸出，其余三指握住。', 8),
    'wrist_flexion': ('掌屈', '前臂保持不动，手掌向前臂掌侧弯曲，手指自然放松。', 3),
    'wrist_extension': ('背伸', '前臂保持不动，手背向前臂背侧弯曲，手指自然放松。', 4),
    'custom': ('自定义动作', '填写你要保持的动作和发力方式。', 0),
}
# NinaPro DB1 exercises A/B/C: the complete 12 + 17 + 23 movement
# vocabulary. Chinese instructions describe a held pose for our recorder;
# functional tasks C21-C23 are represented by their grasp posture.
NINAPRO_ACTIONS = [
    ('A', 1, 'index_flexion', '食指弯曲', '其余手指自然放松，只把食指向掌心弯曲。', 0),
    ('A', 2, 'index_extension', '食指伸直', '其余手指自然放松，只把食指伸直并保持。', 7),
    ('A', 3, 'middle_flexion', '中指弯曲', '其余手指自然放松，把中指向掌心弯曲。', 0),
    ('A', 4, 'middle_extension', '中指伸直', '其余手指自然放松，把中指伸直并保持。', 0),
    ('A', 5, 'ring_flexion', '无名指弯曲', '其余手指尽量放松，把无名指向掌心弯曲。', 0),
    ('A', 6, 'ring_extension', '无名指伸直', '其余手指尽量放松，把无名指伸直并保持。', 0),
    ('A', 7, 'little_flexion', '小指弯曲', '其余手指自然放松，把小指向掌心弯曲。', 0),
    ('A', 8, 'little_extension', '小指伸直', '其余手指自然放松，把小指伸直并保持。', 0),
    ('A', 9, 'thumb_adduction', '拇指靠拢', '手掌保持稳定，让拇指靠向食指一侧。', 0),
    ('A', 10, 'thumb_abduction', '拇指张开', '其余手指放松，让拇指离开手掌、向外张开。', 0),
    ('A', 11, 'thumb_flexion', '拇指弯曲', '把拇指向掌心弯曲，其余手指自然放松。', 0),
    ('A', 12, 'thumb_extension', '拇指伸直', '让拇指从掌心方向伸展开，其余手指自然放松。', 5),
    ('B', 1, 'thumbs_up', '点赞', '大拇指竖起，其余四指握住。', 5),
    ('B', 2, 'two_fingers', '双指伸直', '伸直食指和中指并靠拢，其余手指弯曲。', 6),
    ('B', 3, 'three', '数字三', '伸出拇指、食指和中指，无名指和小指弯曲。', 4),
    ('B', 4, 'thumb_to_palm', '拇指对掌', '让拇指横过掌心，靠近小指根部。', 0),
    ('B', 5, 'open', '张开手掌', '五指自然伸直张开，保持稳定。', 2),
    ('B', 6, 'fist', '握拳', '五指握拳，中等力度，保持每次发力一致。', 1),
    ('B', 7, 'one', '数字一', '只伸出食指，其余四指握住。', 7),
    ('B', 8, 'fingers_together', '五指并拢', '五指伸直，手指互相靠拢，手掌保持平直。', 2),
    ('B', 9, 'supination', '翻掌向上', '肘部贴近身体、前臂向前，把手掌转向上方并保持。', 2),
    ('B', 10, 'pronation', '翻掌向下', '肘部贴近身体、前臂向前，把手掌转向下方并保持。', 1),
    ('B', 11, 'supination_ulnar', '小指侧旋掌向上', '参考图片，以小指侧为轴把手掌向上旋转，保持末端姿态。', 2),
    ('B', 12, 'pronation_ulnar', '小指侧旋掌向下', '参考图片，以小指侧为轴把手掌向下旋转，保持末端姿态。', 1),
    ('B', 13, 'wrist_flexion', '掌屈', '前臂保持不动，手掌向前臂掌侧弯曲，手指自然放松。', 3),
    ('B', 14, 'wrist_extension', '背伸', '前臂保持不动，手背向前臂背侧弯曲，手指自然放松。', 4),
    ('B', 15, 'radial_deviation', '向拇指侧偏腕', '前臂不动，手腕向拇指一侧偏，不要翻转手掌。', 3),
    ('B', 16, 'ulnar_deviation', '向小指侧偏腕', '前臂不动，手腕向小指一侧偏，不要翻转手掌。', 4),
    ('B', 17, 'wrist_extension_fist', '握拳背伸', '先握拳，再把手背向前臂背侧抬起，保持稳定。', 1),
    ('C', 1, 'large_cylinder', '握大圆柱', '像握住粗水瓶一样，拇指与四指环抱物体。', 1),
    ('C', 2, 'small_cylinder', '握小圆柱', '像握住细杆一样，让五指环抱并保持抓握。', 1),
    ('C', 3, 'hook_grasp', '钩状抓握', '四指弯成钩状，拇指放松，像提住袋子把手。', 1),
    ('C', 4, 'index_grasp', '食指伸直抓握', '其余手指握住物体，食指沿物体表面伸直。', 7),
    ('C', 5, 'medium_wrap', '半包围抓握', '像握住中等粗细的瓶子，用掌心与手指包住物体。', 1),
    ('C', 6, 'ring_grasp', '环形抓握', '像拿住横放的小瓶，用拇指与弯曲手指围住物体。', 1),
    ('C', 7, 'prismatic_four', '四指捏握', '拇指与其余四指相对，像捏住直立的小方块。', 4),
    ('C', 8, 'stick_grasp', '握细杆', '把细杆放在掌心，用弯曲四指和拇指握住。', 1),
    ('C', 9, 'writing_grasp', '握笔', '按平时写字的姿势，用拇指、食指和中指握笔。', 4),
    ('C', 10, 'power_sphere', '整手握球', '像握网球一样，掌心和五指共同包住球面。', 1),
    ('C', 11, 'three_finger_sphere', '三指握球', '用拇指、食指和中指托住球面，其余两指放松。', 4),
    ('C', 12, 'precision_sphere', '指尖握球', '五指张开成弧形，用指尖围住小球，掌心留空。', 4),
    ('C', 13, 'tripod_grasp', '三指捏取', '拇指、食指和中指指尖相对，像捏住一个小物体。', 4),
    ('C', 14, 'prismatic_pinch', '扁物捏取', '拇指与伸直的手指相对，像捏住薄盒子的两面。', 3),
    ('C', 15, 'tip_pinch', '两指指尖捏取', '拇指尖和食指尖相碰，其他手指自然放松。', 3),
    ('C', 16, 'quadpod_grasp', '四指捏取', '用拇指、食指、中指和无名指围住小物体，小指放松。', 4),
    ('C', 17, 'lateral_grasp', '侧捏钥匙', '用拇指指腹压住食指侧面，像夹住钥匙或卡片。', 3),
    ('C', 18, 'parallel_grasp', '平行夹持', '拇指与其余伸直的手指相对，像夹住直立的薄书。', 3),
    ('C', 19, 'extension_grasp', '托盘式抓握', '手指接近伸直，拇指从上方固定，像拿住一个小托盘。', 2),
    ('C', 20, 'disk_grasp', '握圆盘', '拇指与四指围住圆盘边缘，像抓住一个宽瓶盖。', 1),
    ('C', 21, 'bottle_cap_grasp', '开瓶握姿', '用拇指、食指和中指捏住瓶盖，保持准备开瓶的握姿。', 4),
    ('C', 22, 'screwdriver_grasp', '螺丝刀握姿', '像使用螺丝刀一样握住手柄，保持抓握姿态。', 1),
    ('C', 23, 'knife_grasp', '持刀握姿', '像握住餐刀一样握住手柄，食指沿刀背方向伸直，保持握姿。', 7),
]
EXCLUDED_ACTION_KEYS = frozenset({'supination', 'pronation', 'supination_ulnar', 'pronation_ulnar'})
NINAPRO_ACTIONS = [row for row in NINAPRO_ACTIONS if row[2] not in EXCLUDED_ACTION_KEYS]

for _, _, key, name, instruction, output in NINAPRO_ACTIONS:
    ACTION_LIBRARY[key] = (name, instruction, output)
NINAPRO_IDS = {row[2]: f'{row[0]}{row[1]}' for row in NINAPRO_ACTIONS}
FOREARM_SCREENING_ACTIONS = [
    'open', 'fist', 'wrist_flexion', 'wrist_extension',
    'radial_deviation', 'ulnar_deviation',
]
FOREARM_SCREENING_PRESET = '手腕七类 · 六动作'
FOREARM_SCREENING_INSTRUCTIONS = {
    'rest': '手指、手腕、前臂自然放松。',
    'open': '五指自然伸直张开，手腕保持平直。',
    'fist': '正常握拳，中等力度，不用捏到发抖。',
    'wrist_flexion': '前臂不动，手掌向手心这一侧弯。',
    'wrist_extension': '前臂不动，把手背向前臂背侧抬起。',
    'radial_deviation': '手腕向拇指方向侧弯，不翻掌。',
    'ulnar_deviation': '手腕向小指方向侧弯，不翻掌。',
    'supination': '肘部位置固定，旋转前臂，让掌心朝上，保持姿态。',
    'pronation': '肘部位置固定，旋转前臂，让掌心朝下，保持姿态。',
}
GROUPS = {
    '手掌手势': ['fist', 'open', 'thumbs_up', 'scissors', 'one', 'six', 'two_fingers', 'three',
                 'thumb_to_palm', 'fingers_together'],
    '手腕动作': ['wrist_flexion', 'wrist_extension', 'radial_deviation', 'ulnar_deviation',
                   'wrist_extension_fist'],
    '手指动作': [row[2] for row in NINAPRO_ACTIONS if row[0] == 'A'],
    '常用抓握': [row[2] for row in NINAPRO_ACTIONS if row[0] == 'C'],
}
PRESET_DESCRIPTIONS = {
    FOREARM_SCREENING_PRESET: '张手、握拳、屈伸与偏腕',
    '原七类 · 手指手势': '熟悉的六种手形',
    '腕部五类 · 优先试验': '握拳、张掌与腕部屈伸',
    '基础三类 · 握拳张掌': '第一次使用，从这里开始',
    '腕部三类 · 掌屈背伸': '手掌向内弯、手背向上抬',
    '腕部五类 · 四方向': '屈伸与左右偏腕',
    '手掌九类 · 常见手形': '手掌与手指组合姿态',
    '手指十三类 · 单指训练': '五根手指的屈伸与拇指张合',
    '拇指七类 · 拇指控制': '拇指屈伸、张合、对掌',
    '抓握七类 · 常见抓握': '圆柱、球、提钩与捏取',
    '精细七类 · 精细捏取': '指尖捏取、握笔与侧捏',
    '生活四类 · 工具握姿': '开瓶、螺丝刀与持刀握姿',
}
OUTPUT_ACTIONS = {
    0: '保持 / 停止触发', 1: '握拳抓握', 2: '五指张开', 3: '两指捏取',
    4: '三指抓握', 5: '点赞手形', 6: '剪刀手形', 7: '数字一手形', 8: '数字六手形',
    **{i: f'自定义机械手动作{i}' for i in range(9, 16)},
}
PRESETS = {
    FOREARM_SCREENING_PRESET: ['rest', *FOREARM_SCREENING_ACTIONS],
    '原七类 · 手指手势': ['rest', 'fist', 'open', 'thumbs_up', 'scissors', 'one', 'six'],
    '腕部五类 · 优先试验': ['rest', 'fist', 'open', 'wrist_flexion', 'wrist_extension'],
    '基础三类 · 握拳张掌': ['rest', 'fist', 'open'],
    '腕部三类 · 掌屈背伸': ['rest', 'wrist_flexion', 'wrist_extension'],
    '腕部五类 · 四方向': ['rest', 'wrist_flexion', 'wrist_extension', 'radial_deviation', 'ulnar_deviation'],
    '手掌九类 · 常见手形': ['rest', 'fist', 'open', 'thumbs_up', 'two_fingers', 'three',
                               'one', 'fingers_together', 'thumb_to_palm'],
    '手指十三类 · 单指训练': ['rest']+[row[2] for row in NINAPRO_ACTIONS if row[0] == 'A'],
    '拇指七类 · 拇指控制': ['rest', 'thumb_adduction', 'thumb_abduction', 'thumb_flexion',
                               'thumb_extension', 'thumb_to_palm', 'thumbs_up'],
    '抓握七类 · 常见抓握': ['rest', 'large_cylinder', 'power_sphere', 'hook_grasp',
                               'tip_pinch', 'tripod_grasp', 'lateral_grasp'],
    '精细七类 · 精细捏取': ['rest', 'tip_pinch', 'tripod_grasp', 'writing_grasp',
                               'lateral_grasp', 'prismatic_pinch', 'precision_sphere'],
    '生活四类 · 工具握姿': ['rest', 'bottle_cap_grasp', 'screwdriver_grasp', 'knife_grasp'],
}


def is_selectable_action(item):
    """Selection policy only; archived models and NPZ label IDs stay untouched."""
    return item.get('key') not in EXCLUDED_ACTION_KEYS and not any(
        word in str(item.get('name', '')) for word in ('翻掌', '旋掌', '旋前', '旋后', '前臂旋转'))


def filter_supported_profile(profile, fallback=True):
    result = copy.deepcopy(profile)
    result['classes'] = [row for row in result.get('classes', []) if is_selectable_action(row)]
    for i, row in enumerate(result['classes']):
        row['id'] = i
    if fallback and len(result['classes']) < 2:
        return preset()
    if len(result['classes']) != len(profile.get('classes', [])):
        result['name'] = '手指与手腕动作'
    return result


def action(key, index):
    name, instruction, output = ACTION_LIBRARY[key]
    return {'id': index, 'key': key, 'name': name, 'instruction': instruction,
            'image': ('ninapro/'+key+'.png') if key in NINAPRO_IDS else key+'.png',
            'reference': NINAPRO_IDS.get(key, ''),
            'output': {'id': output, 'name': OUTPUT_ACTIONS[output]}}


def preset(name=None):
    # Keep the historical fallback for existing users; display priority is
    # independent of the identity stored in recordings and saved profiles.
    name = name or '原七类 · 手指手势'
    classes = [action(key, i) for i, key in enumerate(PRESETS[name])]
    if name == FOREARM_SCREENING_PRESET:
        for row in classes:
            row['instruction'] = FOREARM_SCREENING_INSTRUCTIONS[row['key']]
    return {'version': 1, 'name': name, 'classes': classes}


def validate_profile(profile):
    profile = copy.deepcopy(profile)
    classes = profile.get('classes', [])
    if not 2 <= len(classes) <= MAX_ACTIONS+1:
        raise ValueError(f'请选择1～{MAX_ACTIONS}个动作；静息会自动加入。')
    if classes[0].get('key') != 'rest' or classes[0].get('id') != 0:
        raise ValueError('第0类必须保留为静息。')
    names = []
    for i, row in enumerate(classes):
        if row.get('id') != i:
            raise ValueError('动作编号必须从0开始连续排列。')
        row['name'] = str(row.get('name', '')).strip()
        row['instruction'] = str(row.get('instruction', '')).strip()
        if not row['name'] or len(row['name']) > 30:
            raise ValueError('动作名称需为1～30个字符。')
        if not row['instruction']:
            raise ValueError(f'请填写“{row["name"]}”的动作说明。')
        if i and row.get('key') == 'rest':
            raise ValueError('静息只能出现在第0类。')
        output = row.get('output', {})
        if type(output.get('id')) is not int or output['id'] not in OUTPUT_ACTIONS:
            raise ValueError('机械手动作编号需为0～15。')
        output['name'] = str(output.get('name', '')).strip()
        if not output['name']:
            raise ValueError('机械手动作名称不能为空。')
        names.append(row['name'])
    if len(set(names)) != len(names):
        raise ValueError('动作名称不能重复；请为自定义动作分别命名。')
    if classes[0]['output']['id'] != 0:
        raise ValueError('静息应映射到保持 / 停止触发。')
    # One command code must always have one meaning within the profile.
    outputs = {}
    for row in classes:
        out = row['output']
        if out['id'] in outputs and outputs[out['id']] != out['name']:
            raise ValueError(f'机械手动作码{out["id"]}存在不同名称，请统一含义或换一个动作码。')
        outputs[out['id']] = out['name']
    profile['version'] = 1
    profile['name'] = str(profile.get('name', '')).strip() or '自定义方案'
    return profile


def validate_mapping(mapping, num_classes):
    if len(mapping) != num_classes:
        raise ValueError('机械手映射数量与模型类别数不一致。')
    # Reuse the same constraints as the profile editor, including consistent
    # meanings for command codes and the reserved rest/hold command.
    profile = {'name': '映射检查', 'classes': [
        {'id': i, 'key': 'rest' if i == 0 else 'custom', 'name': f'类别{i}',
         'instruction': '映射检查', 'output': output}
        for i, output in enumerate(mapping)]}
    return [row['output'] for row in validate_profile(profile)['classes']]


def select_profile(profile, ids):
    chosen = {0, *ids}
    result = copy.deepcopy(profile)
    result['classes'] = [row for row in result['classes'] if row['id'] in chosen]
    for i, row in enumerate(result['classes']):
        row['id'] = i
    return validate_profile(result)


def gesture_dict(profile):
    return {row['id']: (row['name'], row['instruction']) for row in profile['classes']}


def load_profile():
    if not PROFILE_FILE.is_file():
        return preset()
    return validate_profile(filter_supported_profile(json.loads(PROFILE_FILE.read_text(encoding='utf-8'))))


def save_profile(profile):
    profile = validate_profile(profile)
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROFILE_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(PROFILE_FILE)


def image_path(row):
    path = Path(row.get('image') or 'custom.png')
    return path if path.is_absolute() else ASSET_DIR/path


def photo_image(row, size=60, master=None):
    from PIL import Image, ImageTk
    path = image_path(row)
    if not path.is_file():
        path = ASSET_DIR/'custom.png'
    if not path.is_file():
        return None
    with Image.open(path) as image:
        image = image.convert('RGBA')
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image, master=master)
