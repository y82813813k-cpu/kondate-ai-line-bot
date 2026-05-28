# Supabase Free setup

## 1. Project

1. Supabase Dashboardを開く
2. New project
3. Organizationを選ぶ
4. Project nameは `kondate-ai-line-bot` など
5. Regionは近い場所を選ぶ
6. Database passwordを保存して作成

## 2. Table

1. 左メニューの SQL Editor を開く
2. New query
3. `supabase/schema.sql` の中身を貼り付ける
4. Run

`bot_state` というテーブルが作られます。
この1テーブルに、在庫・好み・会話履歴・献立履歴をJSONで保存します。

## 3. API keys

1. Project Settings を開く
2. API を開く
3. Project URL をコピー
4. `service_role` keyをコピー

`service_role` keyはサーバー専用の強いキーです。
GitHubには登録せず、RenderのEnvironment Variablesだけに入れてください。

## 4. Render environment variables

Renderの `kondate-ai-line-bot` > Environment に追加します。

```text
SUPABASE_URL=Project URL
SUPABASE_SERVICE_ROLE_KEY=service_role key
SUPABASE_STATE_KEY=global
```

保存後にRenderで再デプロイします。

```text
Manual Deploy > Deploy latest commit
```

## 5. Check

デプロイ後に開きます。

```text
https://YOUR_RENDER_DOMAIN/health
```

以下になればOKです。

```json
{
  "storage_backend": "supabase",
  "supabase_configured": true,
  "storage_ready": true
}
```
