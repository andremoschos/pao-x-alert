"""Repository-wide runtime safety settings.

Python imports sitecustomize automatically during startup when this repository
is on sys.path (as it is in GitHub Actions). Keep the Official PAO ntfy route
pinned to the topic actually subscribed by the user, so an old/mistyped GitHub
secret cannot silently send official alerts somewhere else.
"""

import os

OFFICIAL_TOPIC = "newspao-official-pao-847291"
os.environ["NTFY_OFFICIAL_TOPIC"] = OFFICIAL_TOPIC
