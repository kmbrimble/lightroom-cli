import re
import xml.etree.ElementTree as ET
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "packaging" / "windows"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_wellformed_and_matches_design():
    t = ET.parse(PKG / "lrd-task.xml")
    g = lambda p: t.find(p, NS).text
    assert g(".//t:RunLevel") == "LeastPrivilege" and g(".//t:LogonType") == "InteractiveToken"
    assert t.find(".//t:LogonTrigger", NS) is not None
    assert g(".//t:MultipleInstancesPolicy") == "IgnoreNew" and g(".//t:ExecutionTimeLimit") == "PT0S"
    assert g(".//t:RestartOnFailure/t:Interval") == "PT1M" and g(".//t:RestartOnFailure/t:Count") == "999"
    assert g(".//t:Arguments") == "-m lightroom_sdk.daemon" and g(".//t:Command") == "@@PYTHONW@@"


def test_scripts_use_only_ps51_syntax_and_never_touch_lightroom_ports():
    for f in ("install-lrd.ps1", "uninstall-lrd.ps1"):
        s = (PKG / f).read_text()
        assert not re.search(r"\?\.|\?\?|&&|\|\|", s), f   # PS7-only operators
        assert "54322" not in s and "54323" not in s
