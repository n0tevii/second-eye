"""Conservative checks for explicit RAM/SSD minimums; unrecognized evidence is unknown."""
import hashlib
import re
import unicodedata

_CAPACITY = r'(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB|G|T)(?![A-Za-z])'
_LABELS = {'内存': r'(?:统一内存|内存|RAM)', '内置SSD': r'(?:内置\s*)?(?:SSD|固态(?:硬盘)?|硬盘)'}


def requirement_input_hash(title: str, description: str, requirement: str) -> str:
    return hashlib.sha256(f'capacity-v1\0{title}\0{description}\0{requirement.strip()}'.encode()).hexdigest()


def _gb(size: str, unit: str) -> float:
    return float(size) * (1000 if unit.upper().startswith('T') else 1)


def check_capacity_requirements(title: str, description: str, requirement: str):
    """Return None if no supported minimum is specified, otherwise a tri-state verdict.

    Only explicit labelled capacities or a RAM+SSD pair are accepted. Alternatives,
    negations and upgrade offers never establish the currently offered configuration.
    This gate can reject/withhold a model match, but cannot approve the whole product.
    """
    requirement = unicodedata.normalize('NFKC', requirement)
    limits = {}
    for name, label in _LABELS.items():
        pattern = label + r'(?:\s|\(统一内存\)|容量)*(?:至少|不低于|>=|≥)\s*' + _CAPACITY
        matches = list(re.finditer(pattern, requirement, re.I))
        if matches:
            limits[name] = max(_gb(m['size'], m['unit']) for m in matches)
    if not limits:
        return None
    text = unicodedata.normalize('NFKC', title + '\n' + description)
    # These expressions make it impossible to attribute a capacity to this unit.
    ambiguous = re.search(
        r'可选|可升级|升级至|升级到|可换|最高|起售|仅供参考|实际配置私聊|不是|并非|非\s*\d|或|套餐|多种配置'
        r'|(?:GB|TB|G|T)\s*(?:起|[/／]\s*\d)', text, re.I
    )
    values = {name: set() for name in limits}
    uncertain = set()
    for clause in re.split(r'[，,。；;\n]', text):
        if re.search(r'外置|外接|移动硬盘|硬盘盒|扩展盘', clause):
            # Do not harvest numbers from a clause mixing internal/external storage.
            if re.search(r'内置|SSD|\+|固态', clause, re.I):
                uncertain.add('内置SSD')
            continue
        pairs = re.finditer(r'(?<![\d.])(?P<ram>\d+(?:\.\d+)?)\s*(?:GB|G)?\s*\+\s*' + _CAPACITY, clause, re.I)
        for pair in pairs:
            if '内存' in values:
                values['内存'].add(float(pair['ram']))
            if '内置SSD' in values:
                values['内置SSD'].add(_gb(pair['size'], pair['unit']))
        for name, label in _LABELS.items():
            if name not in values:
                continue
            for pattern in (label + r'\s*(?:容量)?\s*(?:为|是|:)?\s*' + _CAPACITY,
                            _CAPACITY + r'\s*' + label):
                for match in re.finditer(pattern, clause, re.I):
                    values[name].add(_gb(match['size'], match['unit']))
    if ambiguous or uncertain or any(len(v) > 1 for v in values.values()):
        return {'matched': None, 'reason': '容量信息冲突、包含可选配置或无法归属本机，待核验'}
    for name, minimum in limits.items():
        if values[name] and next(iter(values[name])) < minimum:
            actual = next(iter(values[name]))
            return {'matched': False, 'reason': f'{name}为{actual:g}GB，低于要求的{minimum:g}GB'}
    missing = [name for name in limits if not values[name]]
    if missing:
        return {'matched': None, 'reason': '未取得可明确归属本机的' + '、'.join(missing) + '容量，待核验'}
    return {'matched': True, 'reason': '明确标注的内存/内置SSD容量达到门槛；其他需求仍需核验'}
