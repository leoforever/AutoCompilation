"""Skill reconcile 三态 / sync_disk_content 单测（tmp skills 根 + SQLite 内存库）"""
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Skill
import app.services.skill_loader as sl


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    root = tmp_path / "skills"
    (root / "generic" / "sk-a").mkdir(parents=True)
    (root / "generic" / "sk-b").mkdir(parents=True)
    (root / "generic" / "sk-a" / "SKILL.md").write_text(
        "---\nname: sk-a\ndescription: 磁盘新描述\n---\n正文 v2", encoding="utf-8")
    (root / "generic" / "sk-b" / "SKILL.md").write_text(
        "---\nname: sk-b\ndescription: 一致\n---\n正文 same", encoding="utf-8")
    monkeypatch.setattr(sl, "SKILLS_DIR_HOST", root)
    db = Session()
    db.add(Skill(name="sk-a", type="generic", description="旧描述",
                 system_ids=json.dumps([]), skill_content="---\nname: sk-a\n---\n正文 v1"))
    db.add(Skill(name="sk-b", type="generic", description="一致",
                 system_ids=json.dumps([]), skill_content="---\nname: sk-b\ndescription: 一致\n---\n正文 same"))
    db.commit()
    return db


def test_reconcile_detects_mismatch_without_writing(env):
    st = sl.reconcile_all(env)
    assert st["mismatch"] == 1 and st["mismatch_names"] == ["sk-a"]
    assert st["disk_only"] == 0 and st["db_only"] == 0
    # 只报不改：DB 仍是旧文
    a = env.query(Skill).filter_by(name="sk-a").first()
    assert "正文 v1" in a.skill_content


def test_reconcile_all_consistent_zero_mismatch(env):
    b = env.query(Skill).filter_by(name="sk-b").first()
    a = env.query(Skill).filter_by(name="sk-a").first()
    a.skill_content = (sl.SKILLS_DIR_HOST / "generic" / "sk-a" / "SKILL.md").read_text(encoding="utf-8")
    env.commit()
    assert sl.reconcile_all(env)["mismatch"] == 0
    assert b.skill_content  # 未动


def test_parse_description_block_scalar():
    # 多行 description: |（10 篇里 7 篇的写法）——旧行式取到 "|"，应收缩进正文
    multi = ('---\nname: x\ndescription: |\n  第一行说明。\n  第二行触发词。\n'
             'allowed-tools: [execute]\n---\n正文')
    assert sl._parse_frontmatter_description(multi) == "第一行说明。 第二行触发词。"
    # 单行写法不回归
    one = "---\nname: x\ndescription: 单行说明。\nallowed-tools: [execute]\n---\n正文"
    assert sl._parse_frontmatter_description(one) == "单行说明。"
    # description 后直接跟顶格键（块空）→ None 不是 "|"
    empty = "---\nname: x\ndescription: |\nallowed-tools: [execute]\n---\n正文"
    assert sl._parse_frontmatter_description(empty) is None


def test_sync_fixes_block_scalar_description(env):
    # sk-c：库里描述曾被旧解析器写成 "|"，同步应按块标量重析为正文
    (sl.SKILLS_DIR_HOST / "generic" / "sk-c").mkdir(parents=True)
    (sl.SKILLS_DIR_HOST / "generic" / "sk-c" / "SKILL.md").write_text(
        "---\nname: sk-c\ndescription: |\n  多行描述甲。\n  多行描述乙。\ntype: layer1-infra\n---\n正文",
        encoding="utf-8")
    c = Skill(name="sk-c", type="generic", description="|", system_ids="[]", skill_content="旧")
    env.add(c); env.commit()
    # fixture 的 sk-a 本就处于故意不一致状态，同步会把两篇一起刷——断言成员而非全集
    assert "sk-c" in sl.sync_disk_content(env)["synced"]
    assert env.query(Skill).filter_by(name="sk-c").first().description == "多行描述甲。 多行描述乙。"


def test_sync_repairs_pipe_description_when_content_already_matches(env):
    # 正文早已同步、描述却停在旧解析器的 "|"（11-09 生产实况）：再点同步应补析描述
    d = (sl.SKILLS_DIR_HOST / "generic" / "sk-a" / "SKILL.md").read_text(encoding="utf-8")
    a = env.query(Skill).filter_by(name="sk-a").first()
    a.skill_content, a.description = d, "|"
    env.commit()
    out = sl.sync_disk_content(env)
    assert "sk-a" in out["synced"]
    assert env.query(Skill).filter_by(name="sk-a").first().description == "磁盘新描述"
    # 幂等：描述修好后再点无动作
    assert "sk-a" not in sl.sync_disk_content(env)["synced"]


def test_sync_disk_content_refreshes_body_and_description(env):
    out = sl.sync_disk_content(env)
    assert out["synced"] == ["sk-a"]
    a = env.query(Skill).filter_by(name="sk-a").first()
    assert "正文 v2" in a.skill_content
    assert a.description == "磁盘新描述"          # frontmatter 重析
    b = env.query(Skill).filter_by(name="sk-b").first()
    assert "正文 same" in b.skill_content          # 一致的不动
    # 幂等：再同步一次无变化
    assert sl.sync_disk_content(env)["synced"] == []


def test_sync_skips_db_only_rows(env):
    c = Skill(name="sk-c", type="generic", system_ids="[]", skill_content="库里有磁盘无")
    env.add(c); env.commit()
    assert "sk-c" not in sl.sync_disk_content(env)["synced"]
