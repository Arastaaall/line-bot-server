# 栄養管理LINE Bot：システムフローと開発引き継ぎ

最終更新: 2026-08-30

## 目的と運用前提

- LINEで食事の写真・テキストを受け取り、栄養値を推定してGoogle Sheetsへ記録する。
- 直前の食事記録の訂正、カロリー確認、日次振り返り、初期設定にも対応する。
- 雑談は補助機能。短く返答した後、食事記録・栄養相談へ戻す。
- Cloudflare、Render、Google Sheets、LINE、Gemini等は無料枠前提で運用する。新しい有料サービスは追加しない。

## 構成

| 部品 | 役割 |
|---|---|
| `index.ts` | Cloudflare Workers。LINE Webhook受付、署名検証、重複排除、Workers AI分類、雑談返信、Render転送 |
| `main.py` | Render上のFastAPI。Gemini/Groqによる栄養解析、ユーザー状態管理、Sheets保存、LINE返信 |
| `sheets.py` | Google Sheetsの読み書き。`users`、`logs`、`error_logs`、`push_logs`を使用 |
| `wrangler.toml` | Workers、KV、Durable Objects、migration設定 |
| `DAILY_LIMIT_KV` | `webhookEventId`の重複排除のみ。TTLは60秒 |
| `DAILY_LIMIT_DO` | ユーザーごとの雑談日次カウント。JST日付単位 |
| `CHAT_CONTEXT_DO` | ユーザーごとの短期雑談Context |

## 全体フロー

```text
LINE
  ↓ POST /webhook
Cloudflare Workers (index.ts)
  ├─ LINE署名検証
  ├─ webhookEventIdの重複排除
  ├─ 入力中表示
  ├─ テキスト: 雑談/食事intentを判定
  │    ├─ 雑談 → 日次制限 → WorkersからLINE Reply
  │    └─ 食事 → Render /internal/text
  └─ 画像 → Render /internal/image

Render (main.py)
  ├─ ユーザー状態確認
  ├─ Geminiでテキスト分類・栄養値抽出、画像解析
  ├─ 必要時にGemini候補モデル、画像のみGroqへフォールバック
  ├─ Google Sheetsへ保存
  └─ LINE ReplyまたはPush
```

## Workersのテキスト処理

1. `POST /webhook`でLINEイベントを受ける。
2. 署名を検証し、KVで重複イベントを除外する。
3. 「リセット」はContextを消去してRenderへ転送する。
4. 「使い方」「ヘルプ」は固定文を返す。
5. それ以外はLINEのローディング表示を開始する。
6. 次の順に処理する。
   - 食事訂正の明確な表現（食事関連語＋「じゃなくて」「本当は」等）
   - 挨拶、機能質問、テスト発言、不自然な返信への指摘などの定型雑談
   - 上記以外はWorkers AIで`chat`、`meal_add`、`meal_correction`を分類
7. Workers AIが失敗・不正JSON・不正intentの場合は、intentなしでRenderへ送り、Render側の従来Gemini判定へ戻す。

### Workers AIの現行設定

- 既定モデル: `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
- `WORKERS_AI_MODEL`環境変数があればそちらが優先される。古いモデル名が残っていないか確認する。
- `max_tokens: 256`
- `temperature: 0.2`
- タイムアウト: 8秒
- JSON Modeではなく、JSON指示＋コード側の厳格なJSON抽出・検証を使用
- 相手の気持ちを短く受け止める温かい日本語、入力のオウム返し禁止、雑談継続質問の抑制、栄養管理への自然な誘導をプロンプトで指定
- コードフェンス、JSON前後の余分な文章、placeholder、未許可intentを検出する

## 雑談処理

- 雑談返信には、必要な場合だけ話題に沿った柔らかい案内を付ける。正常な返信へ固定案内を機械的に追加しない。

  `食事の記録や栄養相談があれば、いつでも気軽に声をかけてくださいね。`

- 長い会話を促す質問は避け、1〜2文で受け止めて本来機能へ戻す。
- 共感→話題に沿う一言→記録への案内、の順に考える。ただし3段階目は話題と自然につながる場合だけ実施し、天気・相槌・指摘などでは2段階目までで止める。
- 挨拶、機能説明、天候などの短い雑談、テスト、会話品質への指摘などは温かい定型返信を優先し、複数の安全な表現から選ぶ。
- モデルがユーザー入力をそのまま返した場合、システムプロンプトを漏らした場合、空文字やplaceholderを返した場合は安全な案内文へ置き換える。
- 「気分を害したくない」「こちらの立場からも」など、冷たく自己防衛的に受け取られる表現は温かい案内へ置き換える。
- 絵文字は必要な場合だけ0〜1個とし、無理に使わない。同じ絵文字を連続使用しない。
- 雑談の返信は`replyOnlyToLine`を使い、Reply失敗時にPushへフォールバックしない。
- 雑談の成功時だけContextを保存する。Reply失敗時はContextを保存しない。
- 雑談利用回数はデフォルト20回/日。返信末尾に残数を表示する。

### 雑談Context

- `CHAT_CONTEXT_DO`にユーザー単位で保存する。
- 最大8ターン（ユーザー・アシスタント各4回分）。各発言は最大600文字。
- 最終更新から30分を超えると削除する。
- 食事ログやユーザーIDそのものはContextとして渡さず、直近雑談だけをWorkers AIへ渡す。
- `リセット`で削除する。
- Context取得・保存に失敗しても、現在の入力だけで処理を継続する。

## 食事テキスト処理

1. Workersが`/internal/text`へ`user_id`、`reply_token`、本文、食事intentを送る。
2. Renderは内部Secretを検証し、ユーザーごとのロックを取得する。
3. ユーザーが未登録・初期設定中なら、初期設定処理へ進む。
4. 固定コマンドを先に処理する。
5. `meal_add`なら新規食事としてGeminiへ数値抽出を依頼する。
6. `meal_correction`なら`awaiting_correction`と`last_log_id`を確認する。訂正対象がない場合は新規追加に戻すか、正直に案内する。
7. 栄養値を範囲検証し、Google Sheetsの`logs`へ保存する。
8. ユーザーの`last_log_id`、状態、当日累計を更新する。
9. 食事記録はQuick Reply付きReplyを優先し、Reply失敗・Reply token欠落時はPushへフォールバックする。

## 画像処理

1. Workersがローディング表示を開始し、`/internal/image`へ画像message IDを送る。
2. RenderがLINEから画像を取得する。
3. Geminiで画像解析する。Gemini候補が全滅した場合は、画像処理のみGroq Visionを試す。
4. 栄養値を検証して`logs`へ保存する。
5. 食事記録結果をQuick Reply付きReplyで返す。Reply失敗時はPushへフォールバックする。

## Renderのフォールバックと時間制限

- `/internal/text`、`/internal/image`は受信後すぐ`accepted`を返し、実処理はバックグラウンドで行う。
- Geminiのテキスト処理は全体16秒、1モデル6秒、外側のテキスト処理22秒を目安にする。
- 画像処理はGemini全体22秒、1モデル8秒、外側28秒を目安にする。
- テキストのGemini候補は、主に`GEMINI_MODEL`、未使用時は`gemini-3.1-flash-lite`を起点に、環境変数または既定の候補へ進む。
- 無料枠のレート制限・503・404・400・タイムアウトは次候補へ進める。それ以外の通信・JSONエラーは失敗として記録する。
- Render側の最終的な雑談結果はPushせず、Reply専用で処理する。食事結果は従来どおりPushを許可する。

## LINE返信方式

| ケース | Reply成功 | Reply失敗/欠落 |
|---|---|---|
| Workersの雑談 | Replyのみ | 送信せず、ログのみ |
| Renderの最終雑談返信 | Replyのみ | Pushしない |
| 食事追加・訂正 | Reply | Pushへフォールバック |
| 画像解析結果 | Reply | Pushへフォールバック |
| 初期設定・固定コマンド | Reply優先 | 既存の共通フォールバック |

## Durable Objects migration

既存の`v1`は変更しない。`DailyLimitCounter`はv1で作成済みのlegacyクラスとして残す。

現行の方針は次のとおり。

```toml
[[durable_objects.bindings]]
name = "DAILY_LIMIT_DO"
class_name = "DailyLimitCounterV2"

[[durable_objects.bindings]]
name = "CHAT_CONTEXT_DO"
class_name = "ChatContext"

[[migrations]]
tag = "v1"
new_classes = ["DailyLimitCounter"]

[[migrations]]
tag = "v2"
new_sqlite_classes = ["DailyLimitCounterV2", "ChatContext"]
```

- `DailyLimitCounterV2`は既存の`DailyLimitCounter`を継承した新クラス。
- 既存のlegacy DOのstorage backendを変更・削除しない。
- 新しいV2カウンターへ切り替わるため、旧DOのカウントは自動移行されない。切り替え時に日次カウントがリセットされる可能性がある。
- `DAILY_LIMIT_DO`のBinding名は維持しているため、利用側コードのBinding名変更は不要。

## このスレッドで確認・修正したこと

### 問題の確認

- 雑談カウンターが表示されない原因として、Workers AI失敗時に`intent`初期値が`meal_add`へ残り、Renderが食事処理へ進む構造を特定した。
- Workers AIの旧モデル、4秒タイムアウト、自由生成JSONが失敗要因になり得ると整理した。
- Workersログでは明確なエラーが出ず、2〜3秒程度で処理されていた一方、RenderではGemini候補モデルのタイムアウトが記録されていた。
- スイカ画像とおにぎり訂正が別ログになった問題について、食事訂正intent、`awaiting_correction`、`last_log_id`の連携を確認・強化した。

### Workers側

- Workers AIモデル、タイムアウト、出力長、温度を見直した。
- AIエラーにHTTPステータス、本文、タイムアウト、JSON解析失敗の詳細を記録するようにした。
- JSON抽出をコード側で厳格化した。
- 雑談の定型返信と、prompt leak・placeholder・オウム返しの防御を追加した。
- 雑談の定型返信をより温かい表現へ変更し、天候・機能質問・不自然な応答への指摘にも自然に対応するようにした。
- 固定案内文の重複を抑え、「気分を害したくない」など冷たく受け取られる生成文を置き換えるようにした。
- 雑談Context用の`ChatContext` Durable Objectを追加した。
- 雑談日次制限の結果に`count`、`limit`を含め、返信へ残数を表示した。
- 雑談と画像処理の開始前にLINEローディング表示を追加した。
- KVの重複排除TTLを10秒から60秒へ変更し、KV障害時は処理を止めないfail-openにした。
- 雑談はReply専用にし、利用制限DO障害時も共通のPushフォールバックへ流れないようにした。

### Render側

- FastAPIの`app`生成位置、内部認証、重複定義、未定義関数を整理した。
- Workersからの`/internal/text`、`/internal/image`受信後は即時応答し、解析をバックグラウンドで実行するようにした。
- 未処理バックグラウンド例外をログへ残す安全網を追加した。
- ユーザー単位の排他ロックを追加した。
- Workers指定intentを検証し、未知の値を食事追加として誤処理しないようにした。
- Workers AI失敗時のGemini判定フォールバックを追加・整理した。
- 「食事内容なし」を0kcalの食事ログとして保存しない検証を追加した。
- 雑談の最終返信はReply専用にし、Render側からの雑談Pushを停止した。
- Render側のGemini雑談フォールバックにも温かい返信方針と、オウム返し・prompt leak・冷たい定型文の防御を追加した。
- Gemini候補モデルの時間予算、無料枠レート制限、タイムアウト、画像時のGroqフォールバックを整理した。
- `ping`でGET/HEADを許可し、`health`と`health/sheets`を追加した。

## Claudeへ共有するときの重要事項

1. 最初にこのMarkdown、`index.ts`、`main.py`、`sheets.py`、`wrangler.toml`を読む。
2. `wrangler.toml`の`v1`を絶対に書き換えない。
3. `DailyLimitCounter`を削除・改名・SQLite化しない。新しいDOが必要なら新クラスと新migrationを追加する。
4. 雑談を主機能にしない。返信は短く、栄養管理機能へ誘導する。
5. 雑談のReply失敗時にPushを追加しない。食事処理のReply→Pushフォールバックとは分けて考える。
6. APIキー、Channel Secret、内部Secret、Spreadsheet IDなどの実値をコード・ログ・この文書へ書かない。
7. `WORKERS_AI_MODEL`、`GEMINI_MODEL`、`GEMINI_FALLBACK_MODELS`の環境変数がコードの既定値を上書きすることに注意する。

## デプロイ前チェック

- WorkersとRenderを同じ変更内容でデプロイする。
- `v1`を変更せず、`v2`が存在することを確認する。
- `CHAT_CONTEXT_DO`と`DAILY_LIMIT_DO`のBindingが本番へ反映されていることを確認する。
- `WORKERS_AI_MODEL`に廃止モデルが設定されていないことを確認する。
- `こんにちは`、一般的な感想、機能質問、食事追加、直前の食事訂正、画像を順番にテストする。
- 雑談Reply失敗時にPushが発生しないこと、食事処理のReply失敗時にはPushが機能することを確認する。
- Workersログで`workers_ai`、`text_flow`、`daily_limit`、`line_delivery`、`chat_context`を確認する。

## 2026-08-30 追加修正：雑談の温かさ調整と固定コマンドの取りこぼし修正

### 問題の確認

- 雑談の返信が事務的・冷たい印象になっており、以前のGemini運用時と比べて温かさが失われていた。
- 「カロリー」「振り返り」「トータルカロリーの確認」等の入力が、Workers AIの雑談判定で
  「食事の記録を求めていない会話」＝chatと分類され、雑談の日次利用制限を消費していた。
  結果として雑談の利用回数を使い切ると、本来無制限であるべきカロリー確認・振り返りが
  一切できなくなっていた。
- 「使い方」はWorkers側でサーバー状態に依存しない完全静的な短い文言（"食事写真を送ると、
  料理とカロリーを記録します。"）をローカルで即答しており、Render側が持つより詳しい案内文
  （修正方法・リセット方法を含む）が使われていなかった。また「使い方を教えて」のように
  完全一致しない言い回しは、雑談判定側の別ルール（不具合報告などへの定型謝罪文）に
  誤って吸収され、使い方の説明にすらなっていなかった。

### 対応方針

- 「使い方／カロリー確認／振り返り」系の入力は、Workers AI（雑談判定・雑談日次制限）を
  一切経由せず、`リセット`と同様に直接Renderへ転送するようにした（`looksLikeInfoOrStatusCommand`）。
  完全一致の単語リストに加え、「トータルカロリーの確認」のような言い回しも
  件名語（トータル/総合/合計/カロリー）＋動作語（確認/教えて/知りたい 等）の組み合わせで拾う。
- Render側の`handle_common_keywords`も同じ考え方で正規表現による部分一致に対応させ、
  完全一致の文言以外でも実データに基づく回答（`build_today_summary`/`build_today_reflection`）
  や詳しい使い方説明を確実に返せるようにした。
- Workers側のローカル即答（`getFixedCommandReply`）は削除し、「使い方」もRenderの詳しい案内文へ
  一本化した。
- 雑談の返信方針を、共感→（任意で）話題に沿った一言→（関係が自然な時だけ）記録の軽い案内、
  という3段階の考え方に変更した（Workers AIプロンプト・Render側Geminiプロンプトの両方）。
  3段階目は必須にせず、関係の薄い話題では2段階目までで止める。絵文字は必要な場合だけ0〜1個とし、
  同じ表現や絵文字を連続させない。会話を続ける質問はせず、
  自然に会話を終えられる締めくくりにするよう明記した。意味の読み取りにくい入力にも、
  問い詰めずに軽く受け流すよう指示を追加した。
- 挨拶・天候・肯定的フィードバックなど、決定的ルールで返す文言に複数の温かい候補を用意し、
  毎回同じ案内や絵文字にならないようにした。
- 雑談日次制限に達した際の案内文も、突き放した印象にならないよう調整した。

### 影響範囲・確認事項

- Workers AI・Render Geminiいずれの雑談経路も、雑談判定に失敗しても
  「カロリー」「振り返り」等の固定コマンドには到達できるよう、Renderへの転送を
  雑談判定より前段に置いている（`handleTextMessage`内、リセット処理の直後）。
- 正規表現による部分一致は、食事内容そのものの文（例:「今日はラーメンを食べた」
  「カロリー高そうなラーメン食べた」）を誤って固定コマンド扱いしないことを
  簡易スクリプトで確認済み（件名語だけでは反応せず、動作語との組み合わせでのみ反応）。
- 「振り返」を含む文であれば基本的に振り返りコマンドとして扱われる。食事内容の中に
  たまたま「振り返」を含む極端なケース（想定しにくい）では誤反応しうるが、実運用上の
  リスクは小さいと判断した。
- `wrangler.toml`・`sheets.py`は今回変更していない。`v1`は引き続き変更していない。

### デプロイ前チェック（追加分）

- 「カロリー」「振り返り」「トータルカロリーの確認」「使い方」「使い方を教えて」を送信し、
  雑談としてカウントされず、実データ・詳しい案内文が返ることを確認する。
- 雑談の利用回数を使い切った状態でも、上記コマンドが引き続き利用できることを確認する。
- 「こんにちは」「最近寒いね」等の雑談で、温かい返信になっていることと、
  会話を無理に長引かせる質問がないことを確認する。

## 現時点の検証状況

- `git diff --check`: 成功
- `main.py`: Python AST解析成功
- `wrangler.toml`: TOML解析成功
- このローカル環境には`wrangler`と`tsc`がないため、実デプロイとTypeScriptコンパイルは未実行
- Git管理下の`.wrangler/tmp/deploy-vTxREU/`に削除差分がある。今回の本体変更と無関係なら、コミット前に対象外とするか確認する
