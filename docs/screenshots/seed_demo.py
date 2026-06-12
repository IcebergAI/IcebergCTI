"""Throwaway demo-data seeder for README screenshots.

Builds a realistic CTI dataset (analysts, reviewers, stakeholders, notebooks,
sources/notes/attachments, reports across the lifecycle, taxonomy classification,
requirements + tasking, dissemination feeds) in an isolated demo database so the
portal can be screenshotted with believable content. Not part of the app.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ICEBERG_DATABASE_URL", "sqlite:///./demo.db")
os.environ.setdefault("ICEBERG_ATTACHMENTS_DIR", "./demo_attachments")
os.environ.setdefault("ICEBERG_RENDER_OUTPUT_DIR", "./demo_rendered")
os.environ.setdefault("ICEBERG_DEV_AUTH", "true")
os.environ.setdefault("ICEBERG_ENVIRONMENT", "dev")

from pathlib import Path  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from iceberg.db import engine, init_db  # noqa: E402
from iceberg.models import (  # noqa: E402
    Attachment,
    IntelLevel,
    Note,
    Notebook,
    Priority,
    ProductFormat,
    Report,
    ReportStatus,
    Requirement,
    RequirementStatus,
    Role,
    Source,
    Tag,
    TagKind,
    TLP,
    User,
)
from iceberg.services import dissemination, tags as tag_service  # noqa: E402
from iceberg.services.reports import render_report, set_citations  # noqa: E402

UTC = timezone.utc


def dt(days_ago: int, hour: int = 9, minute: int = 0) -> datetime:
    base = datetime(2026, 6, 12, hour, minute, tzinfo=UTC)
    return base - timedelta(days=days_ago)


def main() -> None:
    init_db()  # creates tables, FTS triggers, seeds the starter taxonomy

    with Session(engine) as s:
        # ---- Users ------------------------------------------------------- #
        def user(email, name, role, level=None):
            u = User(email=email, display_name=name, role=role,
                     preferred_intel_level=level)
            s.add(u)
            return u

        alex = user("alex.mercer@iceberg.intel", "Alex Mercer", Role.ANALYST)
        dana = user("dana.cole@iceberg.intel", "Dana Cole", Role.ANALYST)
        priya = user("priya.anand@iceberg.intel", "Priya Anand", Role.REVIEWER)
        sam = user("sam.okafor@iceberg.intel", "Sam Okafor", Role.ADMIN)
        morgan = user("morgan.reyes@northwind.example", "Morgan Reyes",
                      Role.STAKEHOLDER, IntelLevel.STRATEGIC)
        jordan = user("jordan.blake@northwind.example", "Jordan Blake",
                      Role.STAKEHOLDER, IntelLevel.OPERATIONAL)
        riley = user("riley.tan@northwind.example", "Riley Tan",
                     Role.STAKEHOLDER, None)
        s.commit()
        for u in (alex, dana, priya, sam, morgan, jordan, riley):
            s.refresh(u)

        # ---- Tag lookup helpers ----------------------------------------- #
        all_tags = list(s.exec(select(Tag)).all())
        by_ext = {(t.kind, t.external_id): t for t in all_tags if t.external_id}
        by_label = {(t.kind, t.label.lower()): t for t in all_tags}

        def ext(kind, code):
            t = by_ext.get((kind, code))
            if not t:
                print(f"  ! missing {kind.value} {code}")
            return t

        def lbl(kind, label):
            t = by_label.get((kind, label.lower()))
            if not t:
                # tolerant contains match
                for (k, l), tag in by_label.items():
                    if k == kind and label.lower() in l:
                        return tag
                print(f"  ! missing {kind.value} '{label}'")
            return t

        # A campaign (starter taxonomy ships none) — curated by the admin.
        campaign = tag_service.create_tag(
            s, kind=TagKind.CAMPAIGN, label="Volt Typhoon CNI Pre-positioning",
            description="Tracked intrusion set against US critical infrastructure",
        )

        # ---- Notebooks + collection ------------------------------------- #
        def notebook(title, topic, owner, days_ago):
            nb = Notebook(title=title, topic=topic, owner_id=owner.id,
                          created_at=dt(days_ago + 5), updated_at=dt(days_ago))
            s.add(nb)
            s.commit()
            s.refresh(nb)
            return nb

        def source(nb, title, reference, summary):
            s.add(Source(notebook_id=nb.id, title=title, reference=reference,
                         summary=summary))

        def note(nb, body):
            s.add(Note(notebook_id=nb.id, body_md=body))

        def attach(nb, title, filename, ctype, kb):
            data = (f"Demo reference material for {title}.\n".encode()) * (kb * 16)
            out = Path(os.environ["ICEBERG_ATTACHMENTS_DIR"])
            out.mkdir(parents=True, exist_ok=True)
            import uuid
            stored = uuid.uuid4().hex + Path(filename).suffix
            (out / stored).write_bytes(data)
            a = Attachment(notebook_id=nb.id, title=title,
                           original_filename=filename, stored_filename=stored,
                           content_type=ctype, file_size=len(data))
            s.add(a)
            s.commit()
            s.refresh(a)
            return a

        nb_volt = notebook(
            "Volt Typhoon — CNI Pre-positioning", "PRC state-sponsored · OT/ICS",
            alex, 18)
        source(nb_volt, "CISA AA24-038A — PRC State-Sponsored Actors Compromise US CNI",
               "https://www.cisa.gov/news-events/cybersecurity-advisories/aa24-038a",
               "Joint advisory detailing living-off-the-land tradecraft against "
               "communications, energy, water and transportation sectors.")
        source(nb_volt, "Microsoft — Volt Typhoon targets US critical infrastructure",
               "https://www.microsoft.com/security/blog/volt-typhoon",
               "Initial public reporting on LOTL technique reliance and SOHO "
               "router proxy network.")
        source(nb_volt, "Internal telemetry review — DC process-lineage anomalies",
               "INT-2026-0412", "Hunt across managed DCs surfaced anomalous "
               "wmic/netsh execution chains consistent with the advisory.")
        note(nb_volt, "Recurring theme across sources: **no custom malware**. "
                      "Detection has to lean on behavioural analytics, not IOCs.")
        a_volt_pdf = attach(nb_volt, "CISA AA24-038A (archived)",
                            "aa24-038a.pdf", "application/pdf", 180)
        a_volt_ioc = attach(nb_volt, "Edge-device proxy indicators",
                            "volt-proxy-indicators.csv", "text/csv", 12)

        nb_spider = notebook(
            "Scattered Spider — Help-desk Social Engineering",
            "Identity-driven intrusions · SaaS", dana, 11)
        source(nb_spider, "CISA AA23-320A — Scattered Spider",
               "https://www.cisa.gov/news-events/cybersecurity-advisories/aa23-320a",
               "TTP overview: SIM swapping, MFA fatigue, help-desk impersonation.")
        source(nb_spider, "Incident retro — IT service-desk MFA reset abuse",
               "IR-2026-0331", "Adversary called the service desk posing as an "
               "employee to reset MFA and seize the account.")
        note(nb_spider, "Common thread: the *human* help-desk is the weakest "
                        "control. Push for callback verification + manager approval.")
        a_spider = attach(nb_spider, "Help-desk verification checklist (draft)",
                          "helpdesk-verification.md", "text/markdown", 6)

        nb_lockbit = notebook(
            "LockBit Ransomware Ecosystem", "RaaS affiliate tracking", alex, 6)
        source(nb_lockbit, "Affiliate leak-site monitoring — June 2026",
               "OSINT-2026-06", "Victim postings indicate continued affiliate "
               "activity despite prior law-enforcement disruption.")
        source(nb_lockbit, "Sandbox detonation — LockBit builder sample",
               "MAL-2026-0588", "StealBit exfil + self-spreading via SMB; "
               "deletes shadow copies before encryption.")
        note(nb_lockbit, "Watch for `vssadmin delete shadows` and rapid SMB "
                         "enumeration as a pre-encryption signal.")

        nb_phish = notebook(
            "Retail Bank Phishing Surge — Q2 2026", "Credential theft · brand abuse",
            dana, 3)
        source(nb_phish, "Brand-abuse monitoring — lookalike domains",
               "BRAND-2026-Q2", "47 newly-registered lookalike domains spoofing "
               "the retail banking portal in six weeks.")
        source(nb_phish, "Reported phishing lures (customer submissions)",
               "ABUSE-2026-0609", "SMS + email lures citing 'account on hold'; "
               "credential-harvesting kit behind Cloudflare.")
        note(nb_phish, "Kits reuse a common template — pivot on the favicon hash "
                       "and the `/secure-login/` path to find new infra early.")

        # ---- Reports ----------------------------------------------------- #
        VOLT_BODY = """\
## Key judgements

- **Volt Typhoon is pre-positioning, not collecting.** With *high confidence*, the
  group's intrusions into US energy, water and communications networks are intended
  to pre-stage disruptive effects in a future crisis rather than to conduct
  espionage. Observed activity is consistent with maintaining covert, long-term
  access.
- **Living-off-the-land is the defining tradecraft.** The actor relies almost
  exclusively on built-in Windows tooling (`wmic`, `netsh`, `netstat`, PowerShell)
  and valid accounts, leaving minimal malware on disk and blunting signature-based
  detection.
- **End-of-life edge devices are the launch point.** Compromised SOHO routers are
  chained into a covert proxy network so that command-and-control blends with
  legitimate regional traffic.

## Assessment

Since at least mid-2023 the activity cluster tracked as **Volt Typhoon** (overlapping
with *Vanguard Panda*) has run a deliberate campaign against US critical-infrastructure
operators. The intrusions are notable less for their sophistication than for their
**patience and discipline**: the operators prioritise staying hidden over moving fast.

### Initial access and credential theft

Access is typically achieved by exploiting an internet-facing appliance, after which
the actor authenticates using **valid accounts** (`T1078`). Operators then extract
credentials from LSASS memory and the Active Directory database (`ntds.dit`) to enable
quiet lateral movement toward operational-technology management hosts.

### Defence evasion and persistence

The behavioural fingerprint is consistent across victims:

- **Discovery** — built-in `wmic`, `netsh` and `net group`; surfaces as anomalous
  process lineage on domain controllers.
- **Credential access** — LSASS and `ntds.dit` dumps; visible in volume-shadow and
  handle-access alerts.
- **Command-and-control** — a SOHO-router proxy chain; egress to residential ASNs.

The hallmark is restraint: rather than deploying bespoke implants, operators reuse the
victim's own administrative tooling, rotate through compromised credentials, and clean
up artefacts as they go.

## Outlook

We assess with *moderate confidence* that this pre-positioning will **broaden to
additional sectors** over the next 12 months, with water and transportation operators
at elevated risk given their thinner monitoring of OT management planes.

## Recommendations

1. Hunt for anomalous `cmd.exe` / PowerShell parent-child chains on domain controllers
   and jump hosts.
2. Enforce phishing-resistant MFA on every externally reachable administrative
   interface.
3. Prioritise replacement of end-of-life edge appliances and segment OT management
   networks from corporate IT.
"""

        SPIDER_BODY = """\
## Summary

**Scattered Spider** continues to favour the telephone over the exploit. Recent
intrusions begin with a convincing call to the IT service desk, where the adversary
impersonates an employee to **reset multi-factor authentication** and take over the
account — no malware required at the front door.

## Intrusion flow

1. **Reconnaissance** — harvest employee names, roles and reporting lines from
   LinkedIn and data-broker sites.
2. **Help-desk impersonation** (`T1566` adjacent, voice-based) — call the service
   desk, cite stolen personal details, and request an MFA reset or new device
   enrolment.
3. **Account takeover** — authenticate with the reset factor and pivot into SaaS and
   VPN.
4. **Escalation** — target identity infrastructure (IdP admin roles) to mint
   persistence.

## Detection & hardening

- Require **callback to a number of record** plus manager approval for any MFA reset.
- Alert on MFA device changes immediately followed by sign-ins from a new ASN.
- Move high-value roles to phishing-resistant FIDO2 authenticators.

> The control that matters most here is a *process* control on the human help desk,
> not another product.
"""

        LOCKBIT_BODY = """\
## Operational note (draft)

Affiliate activity associated with **LockBit** persists despite prior law-enforcement
disruption. A June builder sample detonated in the sandbox showed the familiar
pre-encryption playbook:

- Deletes volume shadow copies (`vssadmin delete shadows /all /quiet`) to inhibit
  recovery (`T1490`).
- Enumerates and spreads over SMB before encrypting.
- Stages exfiltration via the StealBit utility ahead of `T1486` encryption.

### Early-warning signals

- `vssadmin` / `wmic shadowcopy delete` on servers.
- Rapid SMB share enumeration from a single host.
- Bulk outbound transfer immediately preceding mass file rename.

_TODO: fold in EDR detection coverage gaps before submitting for review._
"""

        PHISH_BODY = """\
## Summary

A surge of **credential-phishing** activity is targeting retail-banking customers via
SMS and email lures themed around account suspension. The campaign leans on
newly-registered lookalike domains fronted by a reverse proxy.

## Indicators of the kit

- Common `/secure-login/` path and a shared favicon hash across domains.
- Cloudflare fronting to obscure origin hosting.
- Real-time relay of one-time passcodes to defeat SMS MFA.

## Recommended actions

1. Submit lookalike domains for takedown as they are registered.
2. Brief the fraud and contact-centre teams on the current lure wording.
3. Encourage customers onto the app's push-based approval rather than SMS OTP.
"""

        def report(nb, title, body, level, tlp, author, days_ago):
            r = Report(notebook_id=nb.id, title=title, body_md=body,
                       intel_level=level, tlp=tlp, author_id=author.id,
                       created_at=dt(days_ago + 4), updated_at=dt(days_ago))
            s.add(r)
            s.commit()
            s.refresh(r)
            return r

        def classify(r, tags):
            r.tags = [t for t in tags if t]
            s.add(r)
            s.commit()
            s.refresh(r)

        def publish(r, reviewer, pub_days_ago):
            r.reviewer_id = reviewer.id
            r.status = ReportStatus.PUBLISHED
            r.published_at = dt(pub_days_ago)
            r.updated_at = dt(pub_days_ago)
            s.add(r)
            s.commit()
            s.refresh(r)
            dissemination.disseminate(s, r)

        # 1) Volt Typhoon — STRATEGIC / AMBER / PUBLISHED  (the sample PDF)
        r_volt = report(nb_volt,
            "Volt Typhoon: Living-off-the-Land Pre-positioning in US Critical Infrastructure",
            VOLT_BODY, IntelLevel.STRATEGIC, TLP.AMBER, alex, 14)
        set_citations(s, r_volt, [src.id for src in nb_volt.sources][:3])
        r_volt.cited_attachments = [a_volt_pdf, a_volt_ioc]
        s.add(r_volt); s.commit(); s.refresh(r_volt)
        classify(r_volt, [
            ext(TagKind.ACTOR, "G1017"), campaign,
            lbl(TagKind.SECTOR, "Energy"),
            lbl(TagKind.SECTOR, "Water"),
            ext(TagKind.TECHNIQUE, "T1078"),
            ext(TagKind.TECHNIQUE, "T1059"),
            lbl(TagKind.TOPIC, "Nation-State"),
        ])
        publish(r_volt, priya, 12)

        # 2) Scattered Spider — TACTICAL / GREEN / PUBLISHED
        r_spider = report(nb_spider,
            "Scattered Spider Help-Desk Social Engineering Playbook",
            SPIDER_BODY, IntelLevel.TACTICAL, TLP.GREEN, dana, 8)
        set_citations(s, r_spider, [src.id for src in nb_spider.sources])
        classify(r_spider, [
            ext(TagKind.ACTOR, "G1015"),
            ext(TagKind.TECHNIQUE, "T1566"),
            lbl(TagKind.TOPIC, "Credential Theft"),
            lbl(TagKind.SECTOR, "Financial Services"),
        ])
        publish(r_spider, priya, 7)

        # 3) Banking phishing — OPERATIONAL / AMBER / APPROVED
        r_phish = report(nb_phish,
            "Retail-Bank Credential Phishing Surge Targeting Customers",
            PHISH_BODY, IntelLevel.OPERATIONAL, TLP.AMBER, dana, 2)
        set_citations(s, r_phish, [src.id for src in nb_phish.sources])
        classify(r_phish, [
            lbl(TagKind.TOPIC, "Phishing"),
            lbl(TagKind.TOPIC, "Credential Theft"),
            lbl(TagKind.SECTOR, "Financial Services"),
        ])
        r_phish.status = ReportStatus.APPROVED
        r_phish.reviewer_id = priya.id
        s.add(r_phish); s.commit(); s.refresh(r_phish)

        # 4) LockBit — OPERATIONAL / AMBER / DRAFT  (the editor screenshot)
        r_lock = report(nb_lockbit,
            "LockBit 3.0 Affiliate Activity — Operational Note",
            LOCKBIT_BODY, IntelLevel.OPERATIONAL, TLP.AMBER, alex, 1)
        set_citations(s, r_lock, [src.id for src in nb_lockbit.sources])
        classify(r_lock, [
            lbl(TagKind.MALWARE, "LockBit"),
            lbl(TagKind.TOPIC, "Ransomware"),
            ext(TagKind.TECHNIQUE, "T1486"),
        ])

        # ---- Requirements + tasking ------------------------------------- #
        def requirement(stakeholder, title, desc, level, prio, status_,
                        days_ago, reports_=(), notebooks_=()):
            rq = Requirement(stakeholder_id=stakeholder.id, title=title,
                             description=desc, intel_level=level, priority=prio,
                             status=status_, created_at=dt(days_ago + 2),
                             updated_at=dt(days_ago))
            rq.reports = list(reports_)
            rq.notebooks = list(notebooks_)
            s.add(rq)
            s.commit()
            s.refresh(rq)
            return rq

        requirement(morgan,
            "Assess nation-state threats to our OT / critical infrastructure",
            "Board-level read on PRC pre-positioning risk to our energy and water "
            "operations, with a 12-month outlook.",
            IntelLevel.STRATEGIC, Priority.CRITICAL, RequirementStatus.IN_PROGRESS,
            16, reports_=[r_volt], notebooks_=[nb_volt])
        requirement(jordan,
            "Detection guidance for help-desk MFA-reset abuse",
            "Practical detections and process controls for service-desk social "
            "engineering of the kind hitting our peers.",
            IntelLevel.OPERATIONAL, Priority.HIGH, RequirementStatus.SATISFIED,
            10, reports_=[r_spider])
        requirement(riley,
            "Early warning on phishing impersonating our retail-bank brand",
            "Continuous monitoring for lookalike domains and lure campaigns "
            "targeting our customers.",
            IntelLevel.OPERATIONAL, Priority.HIGH, RequirementStatus.IN_PROGRESS,
            5, notebooks_=[nb_phish])
        requirement(morgan,
            "Quarterly ransomware landscape briefing for the board",
            "Trends in RaaS affiliate activity and what they mean for our sector.",
            IntelLevel.STRATEGIC, Priority.MEDIUM, RequirementStatus.OPEN, 4)
        requirement(jordan,
            "LockBit affiliate TTP changes affecting EDR coverage",
            "Flag pre-encryption behaviours we should be alerting on.",
            IntelLevel.OPERATIONAL, Priority.MEDIUM, RequirementStatus.OPEN,
            2, notebooks_=[nb_lockbit])

        # ---- Render the sample PDF (Volt Typhoon, full product) --------- #
        try:
            render_report(s, r_volt, ProductFormat.FULL)
            render_report(s, r_spider, ProductFormat.EXEC_BRIEF)
            print("  rendered sample PDFs")
        except Exception as exc:  # pragma: no cover - best effort
            print(f"  ! PDF render skipped: {exc}")

        print("Demo data seeded.")
        ids = {
            "volt_report": r_volt.id, "lockbit_draft": r_lock.id,
            "spider_report": r_spider.id,
        }
        print("IDS", ids)


if __name__ == "__main__":
    main()
