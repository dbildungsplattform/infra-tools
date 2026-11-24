from pathlib import Path
import sys
import json
import click


def rewrite_path(path, new_root):
    # Remove the leading path components so paths are relative to the generated templates.
    return str(Path(new_root, *Path(path).parts[2:]))


@click.command()
@click.option(
    "-r",
    "--rewrite-root",
    metavar="NEW_ROOT",
    help="Rewrite all file paths to be under NEW_ROOT",
)
def main(rewrite_root):
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "kubeconform",
                        "informationUri": "https://github.com/yannh/kubeconform",
                    }
                },
                "results": [],
            }
        ],
    }

    status_map = {
        "statusValid": ("note", "KUBE-VALID"),
        "statusInvalid": ("error", "KUBE-INVALID"),
        "statusError": ("error", "KUBE-ERROR"),
        "statusSkipped": ("note", "KUBE-SKIPPED"),
    }

    data = json.load(sys.stdin)

    for resource in data.get("resources", []):
        file_path = resource.get("filename", "")
        if rewrite_root:
            file_path = rewrite_path(file_path, rewrite_root)
        kind = resource.get("kind", "")
        name = resource.get("name", "")
        status = resource.get("status", "")
        msg = resource.get("msg", "")
        validation_errors = resource.get("validationErrors", [])

        level, rule_id = status_map.get(status, ("error", "KUBE-ERROR"))

        if validation_errors:
            for val_err in validation_errors:
                path = val_err.get("path", "")
                err_msg = val_err.get("msg", "")
                message = f"{kind} {name} is invalid at {path}: {err_msg}"
                sarif_result = {
                    "level": level,
                    "ruleId": rule_id,
                    "message": {"text": message},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": file_path}
                            },
                            "logicalLocations": (
                                [{"fullyQualifiedName": path}] if path else []
                            ),
                        }
                    ],
                }
                sarif["runs"][0]["results"].append(sarif_result)
        else:
            message = msg or f"{kind} {name} has status {status}"
            sarif_result = {
                "level": level,
                "ruleId": rule_id,
                "message": {"text": message},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": file_path}}}
                ],
            }
            sarif["runs"][0]["results"].append(sarif_result)

    json.dump(sarif, sys.stdout, indent=2)


if __name__ == "__main__":
    main()