import json
import re
import sys

# Mapping helm lint level to SARIF allowed values.
level_map = {"error": "error", "warning": "warning", "info": "note"}

sarif = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "helm lint",
                    "informationUri": "https://helm.sh/docs/helm/helm_lint/",
                }
            },
            "results": [],
        }
    ],
}

lint_regex = re.compile(
    r"\[(?P<level>ERROR|WARNING|INFO)\]\s+(?P<file>[^:]+):(?P<message>.*)"
)
line_regex = re.compile(r"yaml: line (\d+):")

for line in sys.stdin:
    match = lint_regex.match(line)
    if match:
        level = match.group("level").lower()
        sarif_level = level_map.get(level, "none")
        file = match.group("file").strip()
        message = match.group("message").strip()
        line_match = line_regex.search(message)
        location = {"physicalLocation": {"artifactLocation": {"uri": file}}}
        if line_match:
            location["physicalLocation"]["region"] = {
                "startLine": int(line_match.group(1))
            }
        sarif["runs"][0]["results"].append(
            {
                "level": sarif_level,
                "message": {"text": message},
                "locations": [location],
            }
        )

json.dump(sarif, sys.stdout, indent=2)
