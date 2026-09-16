# MAIBuilder 共有運用手順

対象構成は **Tailscale Funnel + 独自招待QR + Windowsサービス + Wake-on-LAN + スマートプラグ**。
独自ドメインを購入せず、Tailscaleの固定 `*.ts.net` URLを使う。MAIBuilderは
`127.0.0.1`だけで待ち受け、外部からPCのポートを直接開けない。

## 先に確認すること — データの利用許諾

**この手順で人を招待する前に、JRA-VAN Data Lab. の利用規約を自分で読んでください。**
確認した範囲で、以下が論点になります (2026-09 時点・公開ページより)。

- Data Lab. はデータを取るのに**利用者ごとに会員登録と利用キー (英数字17桁) が必要**で、
  月額 2,090円 (税込) の有料サービス。JV-Link がこのキーで認証する
  — <https://developer.jra-van.jp/t/topic/49>
- 利用規約 **第7条 (その他の利用範囲)** が、本人の個人利用を超えてデータを
  販売・配布・転載・放送する目的で使うことを禁じており、第2項で第三者を通じた
  営業目的の利用も禁じている — <https://jra-van.jp/info/rule.html>
- ソフトを公開して配る場合は **ソフト作者登録** と**ソフトごとの申請**が要る
  — <https://developer.jra-van.jp/t/topic/48>

**この手順書がやっているのは「1つの契約で取ったデータから作った予想を、
招待した第三者に見せること」です。**第7条が想定している場面に当たりうるので、
自分以外に使わせるなら **JRA-VAN に直接確認してから**にしてください。

| 使い方 | 位置づけ |
|---|---|
| 自分だけが使う (`127.0.0.1`) | 個人利用。問題にならない |
| **各自が Data Lab. を契約し、各自の PC で動かす** | ソフトを配る形。作者登録と申請の対象 |
| **招待QRで第三者に見せる (この手順書)** | **要確認。** 第7条の範囲を自分で判断せず、JRA-VAN に問い合わせる |

上の要約は公開ページを読んだもので、法的な助言ではありません。
規約は改定されるので、判断の前に必ず原文を見てください。

## できること・できないこと

- MAIBuilder自身が招待QR・利用者・セッションを管理する。Funnel利用者はTailscaleアカウント不要。
- 登録利用者数にアプリ上の総数上限はない。招待QRごとの利用回数は流出対策として管理者が指定する。
- 利用者ごとにマイAI設定を分離し、同一Windowsサービス内の同時保存を排他制御する。
- Windows起動後はMAIBuilderとTailscaleがサービスとして自動起動する。
- Tailscale Funnelはベータ版で、非公開の帯域制限がある。ホストPC・ルーター・回線・停電まで無停止にはできない。

## 1. Tailscale Funnel

1. Windows版TailscaleをインストールしてPersonalプランへログインする。
2. `tailscale login --unattended` またはクライアントの `Run unattended` を有効にする。
3. 管理画面の Access controls > Node attributes で、管理者ユーザーをTargetにして
   `funnel` 属性を追加する。
4. DNS > HTTPS Certificates でHTTPSを有効にする。
5. MAIBuilderの共有認証が動作していることを確認してから、管理者PowerShellで次を実行する。

```powershell
.\deploy\tailscale\Enable-MAIBuilderFunnel.ps1
```

直接実行する場合:

```powershell
tailscale funnel --yes --bg --https=443 http://127.0.0.1:8780
tailscale funnel status
```

`--bg`で設定したFunnelは、PCまたはTailscaleの再起動後も自動復帰する。停止は
`tailscale funnel --https=443 off`。

参考: [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)、
[Funnelコマンド](https://tailscale.com/docs/reference/tailscale-cli/funnel)、
[HTTPS証明書](https://tailscale.com/docs/how-to/set-up-https-certificates)

## 2. MAIBuilderをWindowsサービスにする

1. `deploy/windows/shared.config.json.example` を同じ場所の `shared.config.json` にコピーする。
2. `python_exe`、`workdir`、`public_url`、`auth_db`を実環境の絶対パスへ変更する。
3. `public_url`にはFunnelが表示した `https://端末名.tailnet名.ts.net` を入れる。
4. `admin_password`を推測不能な長い値（推奨20文字以上）へ変更する。このファイルは共有しない。
5. 管理者権限のPowerShellで実行する。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\Install-MAIBuilderService.ps1
```

状態確認:

```powershell
Get-Service MAIBuilder, Tailscale
Invoke-RestMethod http://127.0.0.1:8780/api/health
Invoke-RestMethod https://端末名.tailnet名.ts.net/api/health
```

ログは `out/logs/service.log`。設定変更後は `Restart-Service MAIBuilder` で反映する。
サービス登録前の設定確認は `python builder/windows_service.py --check-config`、前面起動での確認は
`python builder/windows_service.py --run-config` を使う。

## 3. 招待QRを発行する

1. 公開URLを開いて「管理者ログイン」を押す（または
   `https://端末名.tailnet名.ts.net/?admin=1` を直接開く）。管理者パスワードでログインする。
2. 有効時間と利用可能人数を指定して「招待QRを発行」を押す。
3. スマホ画面の青い「MAIBuilder招待用QR」を相手に読み取ってもらうか、
   「招待URLを共有」で送る。
4. 利用者は表示名を登録すると、発行時に指定した利用期限までログインできる。
   期限到達時または招待の無効化時には自動的に利用できなくなり、再利用には新しい招待が必要。
5. 管理者自身は招待QRを使わず、管理者ログイン後の「MAIBuilderを使う」から利用する。

青いQRはツールへ入るためのもの。投票画面で生成される紫のQRはJRAスマッピー投票用。
QRが漏れた場合は管理画面で即時無効化し、利用者単位でも停止できる。

## 4. スマホからの復旧

1. 公開URLの `/api/health` をスマホで確認する。
2. PCが起動中ならWindowsサービスの回復設定がMAIBuilderを自動再起動する。
3. PCがスリープ中なら、ルーター/NASなど常時稼働機器からMagic Packetを送る。
4. フリーズが疑われる場合だけ、スマートプラグで電源を切り、10秒以上待って入れ直す。

管理者PowerShellで `deploy/windows/Enable-WakeOnLan.ps1` を実行し、BIOS/UEFIの
`Wake on LAN` / `Power on by PCI-E` も有効にする。Wi-Fiではなく有線LANを推奨する。
通常のシャットダウンからのWOLは機種依存なので、運用中はPCをシャットダウンせずスリープさせる。

スマートプラグ復旧には、BIOS/UEFIの `Restore on AC Power Loss` を `Power On` にする。
強制電源断はDBやWindowsを破損し得るため最終手段とし、可能ならUPSを併用する。

参考: [Microsoft: Wake on LAN behavior](https://learn.microsoft.com/en-us/troubleshoot/windows-client/setup-upgrade-and-drivers/wake-on-lan-feature)

## 5. バックアップ

最低限、PC外へ定期バックアップする。

## 速報データの更新間隔

`MAIBuilder Live JRA Data Controller` が1日4回、JV-Dataの開催スケジュールと
当日レース時刻を確認する。非開催日は `MAIBuilder Live JRA Data` を無効化し、
開催日は原則として初回発走2時間前から最終発走2時間後だけ1分間隔で実行する。
祝日・代替開催は曜日ではなく公式開催データで判定する。開催情報の取得範囲が不明な
場合は、取りこぼし防止のため7:00〜21:00の安全側の時間帯を有効にする。

発走15分前の
レースがある間は、各1分実行の30秒後に0B14（馬場・騎手変更・取消／除外）と
対象レースの0B30（全券種オッズ）を追加確認する。

最終成功時刻は `out/cache/live-jvdata-status.json` に保存する。0B14を90秒以内に
確認できていない場合、MAIBuilderは古い出走情報で投票しないようスマッピーQRを
作成しない。画面の「速報確認」は、単なる画面更新時刻ではなくこの成功時刻を示す。

- `out/cache/auth.db`（利用者・招待・セッション）
- `out/cache/configs.json`（利用者ごとのマイAI設定）
- `out/cache/preset_weights.json` と必要な当日データ
- `deploy/windows/shared.config.json`（管理者だけが読める暗号化保管先）

SQLiteはWALを使用するため、単純コピーする場合は一度 `Stop-Service MAIBuilder` してから行い、
コピー後に `Start-Service MAIBuilder` する。月1回は復元テストも行う。

## 費用

**データ元の JRA-VAN Data Lab. が月額 2,090円 (税込)。** これが無いとデータを取れない。
Tailscale PersonalプランとFunnel、MAIBuilder自体には月額費用がなく、独自ドメインも不要。
スマートプラグ・UPSの購入費とホストPCの電気代は別途必要。

以前ここには「月額費用がない」とだけ書いてあったが、**ツール単体の話とデータ元の
契約が混ざって読める**ので分けた。招待する相手にも各自の契約が要るのかは
冒頭の「先に確認すること」を参照。
