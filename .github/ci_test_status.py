"""Keep real test failures fatal except explicitly exhausted paid API credits."""

import os
from pathlib import Path
import sys
from xml.etree import ElementTree


CREDIT_ERRORS = (
    "you have no credits remaining. add credits to continue using the api at https://platform.openai.com/",
    "your credit balance is too low to access the anthropic api",
)


def test_status(report: Path, exit_code: int) -> int:
    if exit_code != 1:
        return exit_code
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError) as error:
        print(f"Cannot classify pytest failures: {error}", file=sys.stderr)
        return exit_code
    failures = [
        (case, failure)
        for case in root.iter("testcase")
        for failure in [*case.findall("failure"), *case.findall("error")]
    ]
    if not failures or len(failures) != len(list(root.iter("failure"))) + len(
        list(root.iter("error")),
    ):
        return exit_code
    unavailable = []
    for case, failure in failures:
        # Only the actual failure message, not captured output or source lines:
        # an unrelated assertion can mention a previous provider error there.
        message = failure.get("message", "").lower()
        if any(error in message for error in CREDIT_ERRORS):
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            unavailable.append(name)
            warning = f"API credits exhausted; live test remains unverified: {name}. Original failure retained in JUnit artifact."
            warning = (
                warning.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            )
            print(f"::warning::{warning}")
    if unavailable and (summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with Path(summary).open("a") as stream:
            stream.write(
                "\n### API credits exhausted\n\nThese live tests ran but remain unverified; their original failures are retained in the JUnit artifact.\n\n",
            )
            for name in unavailable:
                stream.write(f"- `{name.replace('`', '').replace(chr(10), ' ')}`\n")
    return 0 if len(unavailable) == len(failures) else exit_code


if __name__ == "__main__":
    sys.exit(test_status(Path(sys.argv[1]), int(sys.argv[2])))
