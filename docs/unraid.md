# StreamFlow on Unraid

The [Docker template](../templates/streamflow.xml) installs the stable StreamFlow
image through Unraid DockerMan. Community Applications listing is pending
submission and review.

StreamFlow requires an existing Dispatcharr instance reachable from its container.

## Install with DockerMan

Until the template is listed in Community Applications, save
`templates/streamflow.xml` from this repository as
`/boot/config/plugins/dockerMan/templates-user/my-streamflow.xml` on Unraid.
If that filename already exists, keep the existing user template and save this
one with a different `my-` filename.

1. Open **Docker -> Add Container** and select **streamflow** under the user
   templates.
2. Use a unique container name if another StreamFlow instance already exists.
3. Review **WebUI Port** and **Appdata**. The defaults are host port `5000` and
   `/mnt/user/appdata/streamflow`. Separate installations need separate ports
   and data directories.
4. In **Advanced View**, review **PUID** and **PGID** if your share permissions
   require different IDs. Defaults are `99:100`.
5. Click **Apply**, then use the container's **WebUI** action.
6. Complete StreamFlow's setup wizard with the reachable Dispatcharr address and
   your credentials. `localhost` inside this bridge-network container refers to
   StreamFlow itself.

The `/app/data` mapping persists the database, configuration, regex rules and
playback history across container recreation. Keep this mapping when updating.
Change the host port in DockerMan; the container's HTTP port remains `5000`.

## Updates and development builds

Use the normal **Docker -> Check for Updates -> Update** flow. Port, storage and
other container settings remain editable through **Docker -> Edit**.

The default repository is `ghcr.io/krinkuto11/streamflow:latest`. To test a
development build, edit **Repository** to
`ghcr.io/krinkuto11/streamflow:dev`. The template also declares `dev` as an
alternate branch for Community Applications. Review development changes before
updating and keep a filesystem copy of your persistent data before changing
release tracks.

The template uses CPU probing by default and does not request privileged mode.
Optional GPU device/runtime settings can be added in DockerMan's Advanced View;
see the [hardware acceleration guide](operations-guide.md#hardware-acceleration).

For application support, use [GitHub issues](https://github.com/krinkuto11/streamflow/issues).

## Community Applications submission

The public repository contains an Apache-2.0 license, `ca_profile.xml` and one
Docker application template at `templates/streamflow.xml`. The profile, icon,
screenshots, project and support URLs refer to this repository. Screenshots use
example data.

After these files are merged into `main`, sign in to the
[Community Applications submission portal](https://ca.unraid.net/submit/new)
with an Unraid account and enter `https://github.com/krinkuto11/streamflow`.
Run **Validate** and **Scan**, resolve any reported issues and submit the
repository for review. The template's canonical raw URL must be publicly
reachable on `main` before submission.

The catalog scan and moderation decision are separate from local XML checks and
the DockerMan installation test. Do not describe StreamFlow as available in
Community Applications until its listing is published.
