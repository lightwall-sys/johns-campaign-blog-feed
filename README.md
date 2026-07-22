# John's Campaign blog feed

This repository creates a small JSON mirror of recent posts from the official [John's Campaign blog](https://johnscampaign.org.uk/blog/). It is intended for pages that need reliable, structured post metadata without depending on direct browser access to the source website.

The repository publishes three files through GitHub Pages:

- `feed.json`, containing recent post metadata
- `status.json`, containing update status and timestamps
- `index.html`, providing a simple status page

The updater runs every six hours. It also runs when repository files are pushed to the `main` branch and can be started manually from the Actions tab.

If a source update cannot be validated, the last valid feed is retained.

## Initial setup

1. Create a new public GitHub repository named `johns-campaign-blog-feed`.
2. Extract the supplied ZIP file on your computer.
3. Upload the contents of the extracted folder to the root of the repository. Do not upload the ZIP file itself and do not place the files inside an extra parent folder.
4. Commit the upload directly to the `main` branch.
5. Open **Settings**, then **Pages**.
6. Under **Build and deployment**, set **Source** to **GitHub Actions**.
7. Open **Actions**, select **Update feed** and wait for the automatic run to finish. If no run appears, use **Run workflow**.
8. Return to **Settings**, then **Pages** and use **Visit site**.

For a repository owned by `YOUR-USERNAME`, the public files will normally be available at:

- `https://YOUR-USERNAME.github.io/johns-campaign-blog-feed/`
- `https://YOUR-USERNAME.github.io/johns-campaign-blog-feed/feed.json`
- `https://YOUR-USERNAME.github.io/johns-campaign-blog-feed/status.json`

The exact address is shown in **Settings**, then **Pages** after the first successful deployment.

## Manual update

Open **Actions**, select **Update feed**, choose **Run workflow** and run it on the `main` branch.

## Troubleshooting

### The workflow cannot push updated feed data

Open **Settings**, then **Actions**, then **General**. Under **Workflow permissions**, select **Read and write permissions** and save the setting. The workflow also declares the permissions it needs in its own configuration.

### The Pages deployment fails

Confirm that **Settings**, then **Pages**, then **Source** is set to **GitHub Actions**. Open the failed workflow run and inspect the first failed step.

### The scheduled workflow stops running

GitHub may disable scheduled workflows in public repositories after 60 days without repository activity. Open **Actions**, select **Update feed** and choose **Enable workflow** if that option appears. The updater normally commits a status refresh at least monthly to reduce the chance of this happening.

## Files

- `.github/workflows/update-feed.yml` runs the updater and deploys `docs`
- `scripts/update_feed.py` retrieves, normalises and validates recent posts
- `docs/feed.json` contains the last valid feed
- `docs/status.json` reports the most recent update result
- `docs/index.html` provides the public status page
- `requirements.txt` lists Python dependencies

## Licence and source material

The repository code is released under the MIT Licence. Text and images referenced by the generated feed remain the property of their respective owners.
