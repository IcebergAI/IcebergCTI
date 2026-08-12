"""Versioned, wholly synthetic content for the guided intelligence-cycle demo."""

SCENARIO_VERSION = 1
NOTICE = (
    "Synthetic demo — all names, sources, domains, and indicators are fictional. "
    "Nothing here is live intelligence."
)

REQUIREMENT_TITLE = "Synthetic demo: assess Northwind service disruption"
REQUIREMENT_DESCRIPTION = (
    "Determine whether fictional maintenance activity could disrupt the Northwind "
    "service during the next planning window. Use only the supplied synthetic material."
)
NOTEBOOK_TITLE = "Synthetic demo workspace · Northwind"
NOTEBOOK_TOPIC = "Fictional Northwind service continuity assessment"
SOURCE_TITLE = "Synthetic collection note · Northwind maintenance bulletin"
SOURCE_REFERENCE = "urn:iceberg:demo:northwind:bulletin:v1"
SOURCE_SUMMARY = (
    "A fictional maintenance bulletin describes a short service interruption and a "
    "tested recovery procedure."
)
SOURCE_BODY = (
    "This synthetic bulletin states that the fictional Northwind service will enter "
    "maintenance at 02:00 UTC. Recovery was tested in an isolated environment. No live "
    "systems, organisations, domains, indicators, or people are represented."
)
REPORT_TITLE = "Synthetic demo assessment · Northwind service continuity"
REPORT_BODY = (
    "## Assessment\n\nThe supplied fictional bulletin indicates a bounded maintenance "
    "interruption. The assessment uses synthetic material only.\n"
)
KEY_JUDGEMENTS = (
    "We assess the fictional Northwind service is likely to recover within its planned "
    "window, with moderate confidence."
)
KEY_ASSUMPTIONS = "The synthetic recovery test accurately represents the planned procedure."
INTELLIGENCE_GAPS = "No independent synthetic confirmation of the recovery duration is supplied."
