# LINE setup

## 1. Copy channel values

Open LINE Developers Console, then open the Messaging API channel you created.

- `Channel secret`: Channel basic settings
- `Channel access token`: Messaging API settings

Save them in `.env.local`:

```env
LINE_CHANNEL_SECRET=your_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token
```

Keep `.env.local` private. It is ignored by `.gitignore`.

## 2. Enable group usage

In the channel's Messaging API settings:

- Enable webhook
- Enable "Allow bot to join group chats"
- Disable auto-reply messages if the official account manager has them enabled
- Disable greeting messages if you do not want LINE's default greeting

## 3. Deploy the app

Deploy this project to a server that can receive HTTPS requests.

After deployment, set this Webhook URL in LINE Developers:

```text
https://YOUR_DOMAIN/callback
```

Then click "Verify".

## 4. Invite the bot

Invite the LINE Official Account bot into the target group chat.
When the bot joins or receives a message in the group, this app stores the group ID and uses it for the 8:00 dinner suggestions.

## 5. Useful messages

- `献立`: Generate three dinner options
- `微妙`: Generate three alternatives
- `A`, `B`, `C`: Select a dinner option
- `作った`: Save the cooked meal and update inventory
- `在庫`: Show current inventory
- Send a receipt photo: Extract food items and ask before adding them
