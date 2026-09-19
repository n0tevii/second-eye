import pytest

from goodprice.analysis.capacity import check_capacity_requirements

REQUIREMENT = (
    '必须为 Apple Mac Studio 整机，内存（统一内存）至少 128GB，内置 SSD 容量至少 2TB。'
    '不得将外置硬盘、可选升级或其他配置的容量当成本机配置。配置未说明或信息冲突时标记待核验。'
)


@pytest.mark.parametrize(('title', 'description', 'expected'), [
    ('Mac Studio m4max 128+1T', '购入价5.4折', False),
    ('Mac Studio 64GB+2TB', '', False),
    ('Mac Studio 128G+2T', '', True),
    ('Mac Studio', '统一内存256GB，内置SSD 4TB', True),
    ('Mac Studio 128GB+2048GB', '', True),
    ('Mac Studio 128GB+2000GB', '', True),
    ('Mac Studio 128GB+2TB 64GB+1TB', '', None),
    ('Mac Studio 内存128GB', '2TB/4TB SSD', None),
    ('Mac Studio 128GB+2TB起', '', None),
    ('Mac Studio', '512GB 内存，8TB SSD', True),
    ('Mac Studio', '内存128GB，内置SSD1TB，赠送外置硬盘2TB', False),
    ('Mac Studio', '内存128GB，外置硬盘2TB', None),
    ('Mac Studio', '内存128GB，可升级SSD至2TB', None),
    ('Mac Studio', '内存128GB，SSD容量未说明', None),
    ('Mac Studio 128GB+2TB', '实际为64GB内存，1TB SSD', None),
    ('Mac Studio 128GB+2TB 或 64GB+1TB', '', None),
    ('Mac Studio 128GB+2TB', '标题配置仅供参考，实际配置私聊', None),
    ('Mac Studio 128GB+2TB', '不是128GB内存，硬盘非2TB', None),
    ('Mac Studio', '内存128GB，SSD可选2TB/4TB', None),
    ('Mac Studio', '内存128GB，硬盘512GB+外置2TB', None),
])
def test_capacity_evidence(title, description, expected):
    result = check_capacity_requirements(title, description, REQUIREMENT)
    assert result is not None
    assert result['matched'] is expected
    assert result['reason']


def test_unrelated_requirement_has_no_capacity_gate():
    assert check_capacity_requirements('iPhone 128GB', '', '屏幕完好') is None


def test_thresholds_are_read_from_requirement():
    assert check_capacity_requirements('Mac Studio 128G+2T', '', '内存至少256GB，SSD至少4TB')['matched'] is False
