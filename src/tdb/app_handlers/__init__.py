"""Collaborators that hold logic extracted from TdbApp.

Each module here owns a coherent slice of behavior (inspection
workflows, DAP event handling, etc.). The App holds one instance of
each and forwards Textual message handlers and `@work`-decorated
worker stubs to them.
"""
