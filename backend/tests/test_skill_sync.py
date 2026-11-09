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
