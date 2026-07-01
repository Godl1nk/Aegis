"""Security test for iframe sandbox configurations.

Ensures that no iframe combines both 'allow-scripts' and 'allow-same-origin'
in its sandbox attribute, which would allow the sandboxed code to escape.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_iframe_sandbox_security():
    # Scan all HTML, JS files in the static directory
    static_dir = _REPO / "static"
    assert static_dir.exists()

    # Regex to find sandbox attributes in HTML or JS string literals
    # Looks for sandbox="..." or sandbox='...' or sandbox=`...`
    sandbox_re = re.compile(
        r'sandbox\s*=\s*(?P<quote>[\'"`])(?P<value>.*?)(?P=quote)',
        re.IGNORECASE
    )

    for p in static_dir.glob("**/*"):
        if p.is_file() and p.suffix in (".html", ".js"):
            content = p.read_text(encoding="utf-8")
            
            # Find all sandbox attributes
            for match in sandbox_re.finditer(content):
                val = match.group("value")
                # Normalize spaces and check if both exist
                tokens = set(val.lower().split())
                
                # Check for same-origin + scripts combination
                if "allow-scripts" in tokens and "allow-same-origin" in tokens:
                    raise AssertionError(
                        f"Security Vulnerability: File {p.relative_to(_REPO)} contains "
                        f"an iframe sandbox combining both 'allow-scripts' and 'allow-same-origin'. "
                        f"This allows sandbox escape! Match: {match.group(0)}"
                    )
