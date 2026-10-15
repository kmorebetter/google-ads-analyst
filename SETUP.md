# Setup Guide

This guide gets you from zero to your first report. Part 1 takes about 15
minutes and needs no special access. Part 2 is optional. It connects the bot
straight to Google Ads so it can do more.

The bot only reads your data. It never changes your ads, bids, or budgets.

## Before you start

You need:

- A Google Ads account you can log in to.
- Grok Bot with the Google Ads Analyst plugin added.

## Part 0: Add the plugin

### Main way: Grok Bot

1. In Grok Bot, open the plugin search.
2. Search for **Google Ads Analyst**.
3. Click **Install**. Grok Bot shows you what the plugin adds. Approve it.
4. To check it worked, type in chat: **what Google Ads skills do you have?**
   You should see skills like account-setup, csv-import and csv-audit.

Note: this works once the marketplace listing is live. Until then, the plugin
will not show up in search.

### Other way: Cursor on your computer

1. Open a terminal.
2. Copy the plugin into the Cursor plugins folder with this command:

       git clone https://github.com/kmorebetter/google-ads-analyst.git ~/.cursor/plugins/local/google-ads-analyst

3. Reload Cursor. You can quit and open it again, or run the
   **Developer: Reload Window** command.
4. Open the plugin settings and check that Google Ads Analyst is listed.

## Part 1: Start with a CSV file (no API needed)

A CSV file is a simple spreadsheet file. You will download one from Google Ads
and give it to the bot.

### Step 1: Tell the bot about your account

1. In chat, type: **set up my Google Ads account**
2. The bot will ask you some questions in small groups. For example: your
   business name, your customer ID, which campaigns are about your own brand,
   and which conversions matter most.
3. Your customer ID is the 10-digit number at the top right of Google Ads. It
   looks like 123-456-7890.
4. If you do not know an answer, say "skip." The bot will not guess.

The bot saves your answers so every report uses your facts.

### Step 2: Pick your rules for negative keywords

A negative keyword tells Google not to show your ad for a search. The bot will
ask you for four numbers. There are no defaults, because these are your
business choices:

1. **Minimum spend.** How much a search term must cost before the bot will
   suggest blocking it. Example: 50.
2. **Minimum clicks.** How many clicks before a zero is trusted. Example: 10.
3. **Target cost per conversion.** What you are happy to pay for one
   conversion. Example: 120.
4. **Cut multiple.** How many times your target a term can cost before it is
   cut, even if it converts. Example: 3.

### Step 3: Download your search terms report

1. Log in to Google Ads at ads.google.com.
2. In the left menu, click **Insights and reports**, then **Search terms**.
3. At the top right, set the date range to **Last 30 days**.
4. Make sure these columns are showing: Search term, Match type,
   Added/Excluded, Campaign, Ad group, Clicks, Impr., Cost, Conversions. To
   add one, click **Columns**, check the box, and click **Apply**.
5. Click the **Download** icon (a down arrow) and choose **.csv**.
6. The file will land in your Downloads folder.

### Step 4: Upload it and ask for negatives

1. Drag the file into the Grok Bot chat.
2. Type: **find negative keywords in this file**
3. The bot sends you a short summary and two files:
   - **negatives.csv** is the list of suggested negative keywords.
   - **decisions.csv** shows every search term and the reason for its result.

### Step 5: Check before you upload

1. Read the summary. Terms marked HOLD are not negatives yet. They need more
   clicks first.
2. A 30-day file cannot see a sale from 60 days ago. To be safe, download the
   same report for **Last 90 days** and ask the bot to check again.
3. When you agree with the list, upload **negatives.csv** to Google Ads
   yourself. In Google Ads Editor, use **Account > Import > From file**. The
   bot does not do this step for you.

### Step 6 (optional): Get a one-time audit

1. Download these reports for **Last 30 days**, the same way as Step 3:
   - **Campaigns** (left menu: Campaigns). Add the columns Campaign ID,
     Campaign type, Campaign status and Search lost IS (budget).
   - **Ads** (Campaigns, then Ads). Add Ad strength and Headline 1 to Headline 15.
   - **Assets** (Campaigns, then Assets).
2. Drag all the files into chat and type: **audit my Google Ads account**
3. The bot asks for your target cost per conversion if it does not have it yet.
4. You get **audit.md** (what to fix first) and **change-list.csv**.

### Step 7 (optional): Get a weekly review

1. Download the **Campaigns** report twice: once for **last week** and once for
   **the week before**. Both must cover the same number of days.
2. For budget pacing, also download it for **This month** (month to date).
3. Drag the files into chat and type: **write my weekly ads review**
4. Tell the bot your monthly budget if it asks.

That is it for Part 1. You can repeat Steps 3 to 7 every week or month.

## Part 2 (optional): Connect to the Google Ads API

Connecting lets the bot pull data by itself and unlocks every skill: weekly
reviews, ad copy audits, budget pacing, and more. It takes longer because
Google has to approve you.

### What you will need

- A **Google Ads manager account** (free). This is where your developer token
  lives.
- A **developer token** from Google. This is a key that lets software read
  Google Ads data.
- A **Google Cloud project** with OAuth set up, so you can log in safely.
- About 1 hour of setup, plus waiting time for Google's approval. Basic access
  can take a few days to a few weeks.

### Step 1: Create a manager account

1. Go to ads.google.com/home/tools/manager-accounts.
2. Click **Create a manager account** and follow the steps.
3. Link your regular Google Ads account to it. In the manager account, go to
   **Accounts > Sub-account settings**, click the plus button, and send a link
   request. Accept it from your regular account.

### Step 2: Apply for a developer token

1. In the manager account, click **Admin**, then **API Center**.
2. Fill in the form and accept the terms.
3. You will get a token with **test access** right away. Test access only works
   on test accounts.
4. Click **Apply for Basic access**. Describe your use as: "Read-only reporting
   on my own account." Then wait for Google's email.

### Step 3: Set up a Google Cloud project

1. Go to console.cloud.google.com and create a new project.
2. Open **APIs and Services > Library**, search for **Google Ads API**, and
   click **Enable**.
3. Open **APIs and Services > OAuth consent screen** and fill it in. Choose
   "External" and add your own email as a test user.
4. Open **Credentials**, click **Create credentials > OAuth client ID**, and
   pick **Desktop app**. Save the client ID and client secret somewhere safe.

### Step 4: Connect the bot

You have two choices. Pick one.

- **A Google Ads connector (easiest, if one is offered).** In Grok Bot, add the
  connector and sign in with Google when asked. It will ask for your developer
  token.
- **A command-line tool on the bot's computer.** Ask the bot: **install a
  read-only Google Ads command-line tool**. It will walk you through signing
  in. Your keys are saved in a config file on the bot's computer.

Never paste your developer token, client secret, or passwords into the chat
itself. Put them only in the connector or the config file.

### Step 5: Test it

1. Type: **check my Google Ads connection**
2. The bot will read your account name and campaign count. If that works, you
   are connected.
3. Type: **run my weekly Google Ads review** to try it out.

## Getting help

- If the bot says the account profile is still an example, run Step 1 of
  Part 1 again.
- If a report says "section not available," that data could not be pulled. The
  bot will tell you why instead of guessing.
