from __future__ import annotations

import json
import tempfile
from pathlib import Path

from analyzer import DerivedActivity, anonymize_text, build_prompt
from db import StateDB
from dispatcher import dispatch_activities
from scraper import _parse_ndjson_payload, extract_posts_from_graphql


def test_graphql_parser() -> None:
    sample_ndjson = (
        'for (;;);\n'
        + json.dumps(
            {
                "data": {
                    "node": {
                        "__typename": "Story",
                        "post_id": "987654321",
                        "creation_time": 1730000000,
                        "actors": [{"name": "Max Mustermann"}],
                        "wwwURL": "https://www.facebook.com/groups/test/posts/987654321/",
                        "message": {"text": "Kann jemand am Freitag 18 Uhr den Schlüssel mitbringen?"},
                        "feedback": {
                            "total_comment_count": 1,
                            "comments": {
                                "nodes": [
                                    {
                                        "__typename": "Comment",
                                        "author": {"name": "Erika Muster"},
                                        "body": {"text": "Ich bringe ihn vorbei! Tel: +49 170 1234567"},
                                    }
                                ]
                            },
                        },
                    }
                }
            }
        )
    )

    payloads = _parse_ndjson_payload(sample_ndjson)
    assert len(payloads) == 1
    posts = extract_posts_from_graphql(payloads[0])
    assert len(posts) == 1
    post = posts[0]
    assert post["post_id"] == "987654321"
    assert post["author"] == "Max Mustermann"
    assert post["comment_count"] == 1
    assert len(post["comments"]) == 1


def test_anonymizer_and_db_and_dispatcher() -> None:
    text = "Hallo Max Mustermann, schreib an max@example.com oder ruf +49 170 1234567 an."
    anon = anonymize_text(text, ["Max Mustermann"])
    assert "Max Mustermann" not in anon
    assert "max@example.com" not in anon
    assert "1234567" not in anon

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.db")
        csv_path = str(Path(tmpdir) / "activities.csv")
        db = StateDB(db_path)

        changed = db.upsert_post(
            post_id="1001",
            author="Max Mustermann",
            content="Treffen am 2026-12-01 im Vereinsheim.",
            timestamp="2026-11-19T20:00:00+00:00",
            comment_count=0,
            comments=[],
            post_url="https://www.facebook.com/groups/test/posts/1001/",
        )
        assert changed is True

        # Deduplizierung prüfen
        changed_again = db.upsert_post(
            post_id="1001",
            author="Max Mustermann",
            content="Treffen am 2026-12-01 im Vereinsheim.",
            timestamp="2026-11-19T20:00:00+00:00",
            comment_count=0,
            comments=[],
        )
        assert changed_again is False

        pending = db.get_posts_for_analysis()
        assert len(pending) == 1
        prompt = build_prompt(pending[0], anonymize=True)
        assert "Max Mustermann" not in prompt

        activity = DerivedActivity(
            is_actionable=True,
            category="Termin/Event",
            action_title="Treffen im Vereinsheim",
            details="Treffen am 01.12.2026 im Vereinsheim.",
            due_date="2026-12-01",
            priority="Medium",
        )
        db.save_analysis("1001", activity.model_dump())

        dispatched = dispatch_activities(db_path=db_path, csv_output_path=csv_path, webhook_url="")
        assert dispatched == 1
        assert Path(csv_path).exists()
        assert "Treffen im Vereinsheim" in Path(csv_path).read_text(encoding="utf-8")


if __name__ == "__main__":
    test_graphql_parser()
    test_anonymizer_and_db_and_dispatcher()
    print("Alle Pipeline-Tests erfolgreich bestanden!")
