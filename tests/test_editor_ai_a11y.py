"""Focused regressions for report-editor AI review and custom-control semantics."""

import json
from pathlib import Path

from sqlmodel import Session

from iceberg.config import Settings
from iceberg.models import Report
from iceberg.services import ai as ai_service


ROOT = Path(__file__).resolve().parents[1]


def _report(client, login):
    login("ANALYST", email="author@example.com")
    notebook = client.post(
        "/api/notebooks", json={"title": "AI review notebook"}
    ).json()
    return client.post(
        "/api/reports",
        json={
            "notebook_id": notebook["id"],
            "title": "AI review report",
            "body_md": "Analyst-authored assessment.",
        },
    ).json()


def _enable_judgements(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "key_judgements": "AI proposed judgement.",
                                    "key_assumptions": "AI proposed assumption.",
                                    "intelligence_gaps": "AI proposed gap.",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(ai_service.httpx, "post", lambda *args, **kwargs: Response())
    enabled = Settings(
        ai_backend="openai-compatible",
        ai_base_url="https://ai.example.test/v1",
        ai_model="test-model",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: enabled)
    # Endpoints resolve the AI config from the AISettings DB row (#246).
    monkeypatch.setattr("iceberg.services.ai_settings.resolve", lambda session: enabled)


def test_report_editor_exposes_advisory_ai_review_and_aria_controls(client, login):
    report = _report(client, login)

    page = client.get(f"/reports/{report['id']}/edit")

    assert page.status_code == 200
    html = page.text
    assert 'role="tablist" aria-label="Report editor tools"' in html
    assert 'id="editor-tab-ai"' in html
    assert 'id="editor-panel-ai" role="tabpanel"' in html
    assert "Draft judgements" in html
    assert "Suggest tags" in html
    assert "Challenge analysis" in html
    assert 'role="combobox" aria-autocomplete="list" aria-haspopup="listbox"' in html
    assert 'role="listbox"' in html
    assert 'role="option" tabindex="-1"' in html
    assert '@keydown.arrow-right.prevent="moveTab(1)"' in html

    shell = client.get("/").text
    assert 'id="cmdk-dialog"' in shell
    assert 'role="dialog" aria-modal="true" aria-labelledby="cmdk-title"' in shell
    assert 'role="combobox" aria-autocomplete="list" aria-haspopup="listbox"' in shell
    assert 'id="cmdk-list" class="cmdk-list" role="listbox"' in shell


def test_judgement_suggestion_remains_advisory_until_accepted(
    client, login, engine, monkeypatch
):
    report = _report(client, login)
    _enable_judgements(monkeypatch)

    response = client.post("/api/ai/judgements", json={"report_id": report["id"]})

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["suggestion"]["key_judgements"] == "AI proposed judgement."
    with Session(engine) as session:
        saved = session.get(Report, report["id"])
        assert saved.key_judgements == ""
        assert saved.key_assumptions == ""
        assert saved.intelligence_gaps == ""
        assert saved.ai_provenance == {}


def test_editor_client_handles_unavailable_ai_and_focus_restoration_contract():
    script = (ROOT / "src/iceberg/static/js/tags.js").read_text()

    # The UI uses the API's fail-soft envelope and never makes a suggestion a
    # report mutation until a successful ordinary save then provenance stamp.
    assert "!response.ok || !data.available" in script
    assert "Editing remains available." in script
    assert "await this.saveNow()" in script
    assert "/api/ai/accept-provenance" in script
    accept_flow = script[
        script.index("async applyAiReportFields") : script.index(
            "async applyAiJudgements"
        )
    ]
    assert accept_flow.index("await this.saveNow()") < accept_flow.index(
        "/api/ai/accept-provenance"
    )

    # Palette keyboard mechanics are owned by native JS (not inline handlers),
    # including an actual focus trap and restoration to its opening control.
    assert "cmdOpener" in script
    assert "trapCmdFocus(event)" in script
    assert "opener.focus({ preventScroll: true })" in script
    assert "activeDescendant" in script
