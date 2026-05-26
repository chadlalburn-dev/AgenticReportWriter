"""Template authoring adapters — one per entry point.

Each adapter converts a different input shape into a draft
ReportTemplate. They share no abstract base class on purpose: the inputs
are different enough (file vs. directory vs. prompt) that a common base
would carry no shared behaviour.
"""
