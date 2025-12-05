---
name: StreamFlow Agent
description: Github Agent designed for the Streamflow Repo
---

# My Agent

The agent should always do this on any task:

1. Check that the Github Actions cover the pull request's branch that it is working on. The repo owner should be able to test the docker image produced with the changes.
2. Do not create specific MarkDown doc files for that specific feature, always try to incorporate it to existing documentation unless it is worth creating a separate document.
3. Make sure that the repo is clean and sorted out after every implementation. Without redundant/unused code.
4. Always perform code review, testing and linting. Keep the code standard-compliant.
