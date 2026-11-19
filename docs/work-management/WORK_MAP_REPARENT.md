# 既存Workを別の親へ移す

この手順は従来形式のMapで使います。

必要になった時だけ使う手順です。同じWorkのまま配置を変えます。目的自体が別の仕事に変わる場合は、新しいWorkと元Workの扱いを先に判断します。

以下は本人actorが引き受けているWorkを、同じMapの別の親へ移す例です。既存の許可が足りていれば追加設定は不要です。ここでは不足する両端の許可を、実際のOwner指示から限定登録する場合を示します。

## 1. 対象・親・許可を読む

初回に渡されたプログラム、DB、根拠資料の実値を使います。次の設定は、このPowerShell内だけで使います。`$mapRoot`は根拠の`docs`の親で、DBが別位置にあってもその場所のまま指定します。

```powershell
$wm = 'C:\WorkMapPersonal'
$env:PYTHONPATH = "$wm\src;$wm"
Set-Location -LiteralPath $wm
$mapDb = 'C:\Maps\Example\data\workmap.sqlite3'
$mapRoot = 'C:\Maps\Example'
$mapActor = 'actor:example-worker'
python -m tools.read_workmap_store --backend sqlite --store $mapDb --evidence-repo $mapRoot --tree --work W1 --brief --compact
python -m tools.read_workmap_store --backend sqlite --store $mapDb --evidence-repo $mapRoot --work W2 --brief --compact
```

`W1`（移すWork）と`W2`（新しい親）は例です。目的・Origin、子孫への影響、本人のPin、許可、行動ルールと不明点を確認します。新しい親にPinを立てるための手順ではありません。WorkのkeyとOriginは保たれ、表示番号と子孫の住所が変わります。旧番号は別Workに使い回しません。

## 2. 不足する両端の限定許可を作る

同じOwner指示が両端の今回の親変更と許可終了を含んでいる場合の例です。指示は実際の原文に置き換えてください。下の例文自体は許可ではありません。

```powershell
$moveInstruction = 'W1を同じWorkのままW2配下へ移すため、両端の本人限定reparentと今回の許可自体の終了を許可する'
$moveGrantJson = python -m tools.workmap_reparent_permission --backend sqlite --store $mapDb --evidence-repo $mapRoot --actor $mapActor --work W1 --parent W2 --endpoint work --reason '今回の親変更の移動元だけを許可' --owner-instruction $moveInstruction --reviewed-owner-permission
if ($LASTEXITCODE -ne 0) { throw $moveGrantJson }
$moveGrant = $moveGrantJson | ConvertFrom-Json
$parentGrantJson = python -m tools.workmap_reparent_permission --backend sqlite --store $mapDb --evidence-repo $mapRoot --actor $mapActor --work W1 --parent W2 --endpoint parent --reason '今回の親変更の新しい親だけを許可' --owner-instruction $moveInstruction --reviewed-owner-permission
if ($LASTEXITCODE -ne 0) { throw $parentGrantJson }
$parentGrant = $parentGrantJson | ConvertFrom-Json
```

補助が登録・終了するのは今回の対象一件・本人の許可だけです。既存deny・行動ルール・unknown・Owner出典の検査はそのままです。ID・版・時刻・要求JSONは生成されます。フラグは帰属確認で、本人認証ではありません。保存されたPermissionの`reparent`／`remove_permission`は、自由文の制約まで機械的に強制するものではありません。今回の対象・新しい親・今回分の終了という範囲を守る責任は、既存Permissionと同じです。

Owner指示の原文ファイルがすでにある場合は、`--owner-instruction`の代わりに`--owner-source repo:docs/work-management/evidence/owner_実際の資料.txt`を指定できます。根拠の基準は`$mapRoot`です。

## 3. 既存の親変更を使う

手順2で保存された実際のOwner根拠参照を取得し、親変更へ渡します。

```powershell
$savedGrant = Get-Content -Raw -Encoding UTF8 $moveGrant.request_path | ConvertFrom-Json
$moveGrantJsonSource = $savedGrant.request.source_refs[0]
python -m tools.reparent_workmap_work --backend sqlite --store $mapDb --evidence-repo $mapRoot --actor $mapActor --work W1 --parent W2 --reason '実際にこの配置へ変える理由' --source $moveGrantJsonSource
```

出力の`result.result=applied`と`readback.status=confirmed`を確かめ、新しい表示番号・親と、必要な通常Threadから理由を読めるか確認します。許可や判断材料が不足する場合は、既存の正規確認へ戻ります。古い版や不明点を自動的に差し替えません。

## 4. 今回の許可だけを終了する

```powershell
python -m tools.workmap_reparent_permission --backend sqlite --store $mapDb --evidence-repo $mapRoot --actor $mapActor --end-grant $moveGrant.request_path
python -m tools.workmap_reparent_permission --backend sqlite --store $mapDb --evidence-repo $mapRoot --actor $mapActor --end-grant $parentGrant.request_path
```

元の保存要求と確定済みの記録、現在の完全なPermission値を照合し、今回作成したIDだけを終了します。他の許可やPinを削除しません。親変更が止まった場合も、今回の許可を残す必要がなければこの手順で終了できます。既にない場合は、新たな保存成功とは区別して`already_absent`を返します。

応答が途切れた時は、新しい許可を作り直さず、出力された`request_path`を使って`--resume <そのファイル>`で確認します。grantの再開には同じ`--reviewed-owner-permission`が必要です。親変更の再開には親変更が返した要求ファイルを使います。いずれも同じDB・backend・根拠基準を使い、競合なら現在を再読して判断します。

PowerShell変数を失った時は、DBと同じフォルダの`transition-requests/`から今回の`*-permission.json`を探します。中断後はまず移すWorkと新しい親を上の`--brief`で読み、`permission:reparent:`で始まる今回分が残っているか確認します。担当が変わった場合は元actorを名乗らず、元要求・receiptと現在の完全なPermission値を引き継ぎ、Ownerの実際の指示に基づく通常の`remove_permission`で処遇します。関係ない許可をまとめて消しません。

親変更本体も、未適用の再開時には準備時のローカル根拠版を照合します。版を持たない旧要求は自動再開せず、元のID・本文・版を保って現在から判断します。適用済みなら元のreceiptを回収します。要求はこの回収に必要な間は保ち、今回の手順で自動削除は行いません。
