# 共用プログラムを保守する方へ

普段の利用は[USAGE](USAGE.md)のままです。ここでは、PCで共用するプログラムの設定と更新を扱います。MapのDB・根拠資料・保存要求は、それぞれのMapの場所に保ちます。

保守コマンドには、PythonとPowerShell 7（`pwsh`）を使います。普段の利用のために、保守コマンドを毎回実行する必要はありません。

## 初回だけの共用設定

新しい配布一式を同期対象外の場所へ展開し、使うプログラムの場所へ配置します。このPCの全ユーザーで共用するなら、例えば `C:\Program Files\Work Map` を使えます。配置・更新は管理者権限で行い、利用する人は通常権限で起動します。共用フォルダは、通常の利用者が書き換えられない権限に保ってください。

設定には、未使用のプログラム一式を使います。プログラムの中にMapを作らず、以後の操作は `--map-dir` で各Mapを指定します。既存Mapを使う場合も、その保存先を指定し、初期化し直しません。

最初は、保守用の材料を作ります。以下の変数は例なので実際の場所へ置き換えてください。`$bundle` はまだ存在しないフォルダを選びます。

```powershell
$program = 'C:\Program Files\Work Map'
$bundle = Join-Path $env:LOCALAPPDATA 'Work Map\maintenance\first'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $bundle) | Out-Null
$plan = python "$program\maintenance\prepare.py" enable --program "$program" --output "$bundle" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '共用設定の準備を確認してください。' }
$expected = $plan.bundle_sha256
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected
```

この段階ではプログラムを変更しません。返された対象と材料を確認し、**初回だけ、旧版を含む利用中の画面・処理の終了を合わせ、新しく起動しない区切り**を取ります。次を管理者のPowerShellから実行します。

```powershell
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected -Mode Apply -UsageStopped
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected
```

設置IDと場所を結び付けた `INSTALLATION.json` と `runtime-use.lock` が作られます。配布ZIPには各PCの設置IDを入れていません。設定後は、通常権限で別の使い捨てMapの読取りを確認してから利用を再開します。

## 通常の更新

新しい一式を別の同期対象外フォルダへ展開し、検証してから使います。現在の共用版や、使っているMapの上へ直接展開しないでください。

```powershell
$candidate = 'D:\Downloads\Work Map candidate' # 新しい一式の場所
$bundle = Join-Path $env:LOCALAPPDATA 'Work Map\maintenance\next'
$plan = python "$program\maintenance\prepare.py" update --program "$program" --candidate "$candidate" --output "$bundle" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '更新範囲を確認してください。' }
$expected = $plan.bundle_sha256
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected
```

材料には、対象場所・設置ID・全対象ファイルの変更前後のhashと、変更分だけの元／先の内容が入ります。プログラム全体やMapのバックアップを毎回作る処理ではありません。**この材料は設置先用です。GitHubの配布物や他の設置先へ混ぜないでください。** `bundle_sha256` と材料の場所は、結果と一緒に残します。PowerShellを開き直した場合も、元の値を使います。

範囲を確認後、管理者のPowerShellで反映します。

```powershell
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected -Mode Apply
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected
```

対応済みの利用処理が残っていれば、変更せず `busy` で戻ります。開いている画面やAPIホストが自然に終了する区切りを待ちます。通常更新のたびに全チャットへ停止・再開の返答を求める必要はありません。更新中に起動した利用側は、Work操作を始めずに案内を返します。

## 初回の切替・固定入口を変える更新

旧版がこの仕組みに参加しない場合や、更新前に読まれる入口・保守処理自身を替える場合は、通常の排他だけでは足りません。準備時に固定入口の違いを伝えられたら、利用停止の区切りを別に用意し、次のように材料を作ります。

```powershell
$plan = python "$candidate\maintenance\prepare.py" update --program "$program" --candidate "$candidate" --output "$bundle" --coordinated | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '初回切替の対象と元の状態を確認してください。' }
$expected = $plan.bundle_sha256
# 利用終了と、新規起動を控える区切りを確認した後だけ実行します。
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected -Mode Apply -UsageStopped
```

`-UsageStopped` は、その区切りを確認した操作者の入力です。Pinの状態やプロセス一覧だけで、全利用が終了した証明にはしません。この区切りは初回・固定部分の変更時に使います。

## 中断した時と元へ戻す時

応答が途切れた時は、新しい材料を作り直さず、**同じ材料と同じhash** の既定Verifyで現在を確認します。

- `state: after` で、同じ更新の中断印だけが残っている場合は、同じ材料のApplyで完了を確定できます。
- `mixed` ならApplyを重ねず、同じ材料のRestoreで元へ戻して確認します。
- 知らないファイル、後からの編集、異なる設置ID・材料・hashがある時は保全して止まります。時刻やファイル名だけで一時ファイル・中断印を削除しません。

```powershell
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected -Mode Restore
pwsh -NoProfile -File "$bundle\run.ps1" -ExpectedBundleHash $expected
```

初回・固定部分の材料を戻す時も、利用停止の区切りを保ち、Restoreへ `-UsageStopped` を付けます。元のファイルと、新規追加前の不在を確認してから利用を再開します。MapのDBを過去へ巻き戻す操作ではありません。

正常完了は終了0、通常の失敗後に元の状態まで戻せた時は終了1、点検が必要な拒否は終了2、利用中は終了3です。更新や復旧が未完了の間は、`UPDATE_PENDING.json` によって新しい利用を止めます。復元の必要がある間は材料を保ち、不要になった時にその材料だけを片付けます。
