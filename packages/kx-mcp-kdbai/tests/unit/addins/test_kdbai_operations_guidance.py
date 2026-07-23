"""Resource: operations guidance reads its packaged text.

The bare-URI registration is covered by ``test_kdbai_addins_init.py`` (native discovery of the
standalone ``@resource`` component); here we only pin the impl reads its co-located text.
"""

from kx_mcp_kdbai.addins import kdbai_operations_guidance as mod


def test_impl_reads_packaged_guidance():
    text = mod.kdbai_operations_guidance_impl()
    assert isinstance(text, str) and len(text) > 0
