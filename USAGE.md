# Work Map／Work Threadを使い始める

考え方と一続きの利用例は[README](README.md)にあります。この資料は、初期設定と必要な操作を調べるための手順です。毎回全文を読む必要はありません。

## 最初にすること

新しく始めるときはMapを作り、すでにMapを持っている場合はその保存先を指定します。

1. ZIPを利用するWindows PCの**同期対象外のローカルフォルダ**へ展開します。例：`C:\WorkMapPersonal`。展開された`workmap.py`と`README.md`のあるフォルダを使います。OneDrive、共有フォルダ、ネットワークドライブ上では運用しません。
2. エクスプローラーでそのフォルダを開き、上部のアドレスバーに`powershell`と入力してEnterを押します。開いたPowerShellで`python --version`を実行し、**Python 3.11以上**と表示されるか確認します。
   Pythonが見つからない、または3.11未満の場合は、[Python公式ダウンロードページ](https://www.python.org/downloads/)からWindows用を入手してインストールし、PowerShellを開き直してもう一度確認します。詳しい手順は[公式のWindows導入案内](https://docs.python.org/3/using/windows.html)にあります。
   続けて`python -m tkinter`を実行し、小さなウィンドウが開けば画面表示の準備もできています。確認したらそのウィンドウを閉じます。追加ライブラリは不要です。
   ウィンドウが開かない場合は、表示されたエラー文を控えます。`tkinter`や`_tkinter`が見つからない場合は、利用中のPythonの提供元で、画面表示用のTcl/Tkの追加・修復方法を確認してください（[Tkinter公式案内](https://docs.python.org/3/library/tkinter.html)）。`python`コマンド自体が動かない場合は、[Windows版のトラブルシューティング](https://docs.python.org/3/using/windows.html#troubleshooting)を参照してください。
3. プログラムの場所と、使うMapの場所を一度指定します。次は**新しいMapを作る場合**の例です。既存Mapならその保存先を指定し、`init`は実行しません。名前などは自分の仕事の言葉へ直してください。AIに実行させる場合も、本人が実際に承認した登録内容を渡します。

```powershell
$wm = 'C:\WorkMapPersonal'
$mapRoot = 'C:\Maps\Example'
function wm { python "$wm\workmap.py" --map-dir $mapRoot @args }
wm init --name "自分の仕事" --intent "担当業務の目的と判断理由を保ち、確実に引き継げるようにする" --origin "自分の仕事でWork Mapを使い始める" --owner-instruction "この名前・目的・由来で、自分の仕事の親Workを登録する"
wm gui
```

`wm`はこのPowerShell内だけで使う短い呼び方です。以後の例も同じプログラムとMapを使います。新しいPowerShellでは上の最初の3行を使うか、導入担当から渡されたMap別の起動ファイルを使います。プログラムをMapへコピーする必要はありません。

最初のWorkはW0です。`--owner-instruction`には、この内容で登録すると決めたことを書きます。既存DBは初期化で上書きされませんが、保存先を間違えた場所に別Mapを作ることはできるため、読めない既存Mapを`init`で直そうとしないでください。

迷った時は、[最上位Work・目的・きっかけを書くヒント](docs/work-management/WORK_MAP_START.md#最上位のworkを考えるヒント)を使えます。

新形式では、現在の仕事と判断に必要な理由・結果を残します。初期設定した編集者は、すぐにW0の下へWorkを登録できます。閲覧は許可設定なしで始められます。ほかの操作は使う時に設定します。

<a id="shared-program"></a>

## 同じプログラムで別のMapを使う

別Mapを使う時は、同じプログラムのまま保存先を替えます。場所は**絶対パス**で指定します。次は別の既存Mapを読む例です。

```powershell
$mapRoot = 'C:\Maps\Another'
wm show
```

各Mapフォルダの下にDB（`data/workmap.sqlite3`）、根拠（`docs`）、GUIの接続一覧を置きます。DBがこの位置と異なる既存Mapは、その場所を変えず、[明示したDBと根拠を使う入口](docs/work-management/WORK_MAP_START.md#接続情報を一度渡す)を使います。

`wm`は毎回この`$mapRoot`を渡します。GUIで別Mapを選んでも、コマンドの対象は変わりません。Mapを明示したGUI起動は、そのMapと根拠の場所を優先し、覚えている他Mapは切替候補として残します。以下の例は使いたいMapの`$mapRoot`で続けます。

直接呼ぶなら`python "$wm\workmap.py" --map-dir $mapRoot show`、GUIだけなら`python "$wm\WorkMap.pyw" --map-dir $mapRoot`です。従来のコードとDBが同居する構成では、`--map-dir`なしの起動やダブルクリックも使えます。指定なしはプログラムのフォルダを使う互換動作で、Program Filesの共用版では使いません。保存先の指定は操作許可や新しいMapの作成指示ではありません。

## GUIで開く

使うMapを指定した後は、次のコマンドで開きます。

```powershell
wm gui
```

左の木からWorkを選ぶと、詳細と判断理由を読めます。担当別表示や、自動更新のON／OFFも画面から使えます。「Mapを閉じる」は表示対象から外す操作で、データは残ります。

編集者の初期表示はOwnerです。自分以外の担当が使う時は、その担当の名前と許可を確かめてください。Pin操作やWorkの終了は、後述のコマンドで行います。

## 仕事を登録する

```powershell
wm add --parent W0 --name "月次報告を整える" --intent "報告を受ける人が状況と必要な判断を理解できるようにする" --origin "月次報告の担当になった" --owner-instruction "この仕事をW0の子として登録する"
```

画面を開いたままなら、画面へ戻って**F5で再読込**します。W0の下に、追加したW1が表示されます。自動更新をONにしている場合は、その更新でも反映されます。

`--parent`には、Mapで確認した親Workの番号を指定します。登録内容を保存し、読み戻した結果が表示されます。登録しただけでは、その仕事を引き受けたり、実行を許可したことにはなりません。

気になる案も登録しておけます。目的ときっかけはWorkに残ります。後から判断に必要になる理由は、[理由を残す手順](#引き受ける理由を残す終了する)でThreadへ記録します。

### 登録したWorkの操作を許可する

新しいW1を自分で扱う時は、そのWorkで使う操作を別に選びます。Workを登録しても、編集や終了まで許可されたことにはなりません。次は訂正・記録・Pin操作・終了をすべて使う場合の例です。使う操作だけを指定してください。

```powershell
wm permit --work W1 --edit --record --pin --close --owner-instruction "W1の訂正、理由と結果の記録、本人Pin操作、終了に必要な確認を済ませた場合の終了をOwnerに許可する"
```

### 画面から追加・訂正する準備

自分のMapでも、「誰が操作するか」と「何をしてよいか」は分けて記録します。人とAIが同じMapを使った時に、担当になったことを、何でも変更してよいという許可と取り違えないためです。一人で使う場合は、自分で必要な操作を選びます。

新しく作ったMapでは、初期設定した編集者が、自分の操作を対象ごとに許可できます。既定の表示名はOwnerです。これは操作範囲の記録です。利用者の識別やファイルへのアクセスはPC側で管理し、AIには本人が決めた範囲と実際の指示を渡します。

画面から新しいWorkを追加する時は、一度だけ、このMapへの追加を許可します。

```powershell
wm permit --work W0 --map-add --owner-instruction "このMapの現在・今後のWorkの下への新しいWork追加をOwnerに許可する"
```

許可はMapとDBの保存場所に結び付いています。プログラムだけを更新しても、同じDBの許可は使えます。Map自体を移す必要がある時は、元DBと根拠を保全して確認してください。

今のGUIでは、追加内容の根拠になる文書も必要です。`docs`配下に、登録する名前・目的・生まれたきっかけを本人が採用したことを書いたテキストを保存し、「資料を選ぶ」から指定します。手元に文書を用意せず追加したい時は、上の「仕事を登録する」のコマンドを使えます。入力した登録指示から、必要な記録を作ります。

既存Workの名前・目的・出自を画面で訂正する時は、対象Workへの編集を許可します。

```powershell
wm permit --work W0 --edit --owner-instruction "W0の名前・目的・出自の訂正をOwnerに許可する"
```

目的の訂正・明確化には、`docs/work-management/evidence/owner_任意名.txt`へ本人の判断、変更前後、理由、同じ目的である根拠を書いて選択します。追加・訂正のどちらも、反映前に表示される対象と内容を確認してから保存します。ここで設定した編集許可が、別のWorkへ自動で広がることはありません。

## 現在地と理由を読む

- `show`：現在の仕事の木。
- `show --work W1`：目的、親、担当、許可、行動ルール、依存、理由など。
- `thread` または `thread --work W1`：将来の判断に必要な意味だけ。

通常の登録直後にThreadが空でも異常ではありません。生まれた理由はWorkの目的ときっかけにあります。未確認の項目は、確認した結果「何もない」場合と区別して扱います。

AIへ必要な現在情報だけ渡す時は `wm show --work W1 --brief` を使えます。Workの目的・親の目的・担当・許可・行動ルールと理由／結果の索引を返します。`wm`を用意していない環境では、上記の絶対パスの直接呼出しを使います。

その続きは、必要なものだけを次のコマンドで読みます。

```powershell
wm show --work W1
wm thread --work W1
```

順にWorkの詳細とWork Threadです。必要な方を使います。各コマンドは、実行した時点の最新を読みます。複数の結果を組み合わせて判断する時は、同じ版か確認します。判断や更新の前には、現在の目的・許可・行動ルール等を確認します。

AIや独自の連携から、同じ版の記録一件だけを追加で読みたい場合は、[絞り込んで読む方法](HANDOVER.md#同じ版の記録を絞り込んで読む)を使います。通常の閲覧は、上の画面やコマンドで始められます。

## 引き受ける・理由を残す・終了する

```powershell
wm pin --work W1 --reason "月次報告の確認を主作業として引き受ける" --owner-instruction "本人の主作業をW1にする"
wm reason --work W1 --text "受け手の判断に必要な内容を先に確認し、装飾は後で扱う" --owner-instruction "この判断理由を残す"
```

別のWorkへも同じ`pin`で切り替えます。引き受けるWorkには`permit --pin`で操作範囲を設定しておきます。同じ許可が有効な間は、切替のたびに設定する必要はありません。行動ルールや依存関係で止まったら、その内容と根拠を確認します。

`reason`は将来の判断に必要な「なぜ」です。試験・保存・成果の記録は、実際の根拠ファイルを作って `result` で接続します。結果を記録しても、ほかの残っている仕事が終わったことにはなりません。

```powershell
wm result --work W1 --text "報告内容の確認結果" --source repo:docs/work-management/evidence/report.txt
wm close --work W1 --outcome completed --reason "元の目的を達成し残務を処遇した理由" --evidence repo:docs/work-management/evidence/report.txt --release-own-pin --owner-instruction "根拠と現在状態を確認してW1をcompletedで閉じ、本人Pinを解除する"
```

`report.txt`は例です。実際の成果と判断を確認できるファイルへ置き換えます。達成した仕事は根拠を指定して`completed`、見送った仕事は`stopped`で終了します。子Workや他の担当、依存・行動ルールが残る場合は、先にその扱いを考えます。終了後も、Workの一意のIDを使えば最終の目的と判断理由をThreadから読めます。引き続き別Workを担当する時は、そのWorkを指定して`pin`します。

## 応答が分からなくなったとき

まず`wm show`で現在のMapを見ます。登録や更新の応答が途切れたら、同じ操作をやり直す前に、対象を確認してください。Workが分かっていれば、例えば`wm resume --work W1`で現在の内容と理由・結果を読めます。終了済みWorkは一意のIDを使います。前の要求が実行されたかは未確認なので、読めた内容をもとに続け方を判断します。初期化途中で読めない時は、保存済みのDB・根拠を保全し、新しいフォルダで始めてください。

## バックアップと復旧確認

バックアップ先をMapとは別の場所に作ります。

```powershell
New-Item -ItemType Directory C:\WorkMapBackups\day1
wm backup --output C:\WorkMapBackups\day1\backup.json
```

確定した一版と、確認した根拠が保存されます。バックアップと、表示されたSHA-256（確認値）を一緒に保管してください。必要な外部資料もそれぞれの保管先を確認します。

```powershell
wm recover-check --backup C:\WorkMapBackups\day1\backup.json --sha256 "出力されたSHA-256" --target C:\WorkMapBackups\day1\candidate.sqlite3
wm recovery-show --candidate C:\WorkMapBackups\day1\candidate.sqlite3 --work W0 --thread
```

候補は読取専用です。必要なWorkの目的、理由、結果、根拠を確かめます。元のDBが開けない場合も、バックアップから候補を確認できます。

### 元の保存先で再開する

バックアップ以降の変更、現在の担当と許可を確認し、この候補で再開するか決めます。更新を止め、元のDB・SQLiteの付随ファイル・根拠を保全して、空いた元の保存先を指定します。下の確認値はバックアップと候補の値に置き換えてください。候補の確認結果の`project_id`と`candidate_token`を使います。

    wm restore --backup C:\WorkMapBackups\day1\backup.json --candidate C:\WorkMapBackups\day1\candidate.sqlite3 --sha256 "バックアップのSHA-256" --candidate-token "候補の確認値" --project-id "確認したMapのproject_id" --owner-instruction "この候補の内容と担当・許可を確認し、保全した元の保存先で再開する"
    wm show --work W0 --brief

空いた元の保存先に新しい版を作ります。そこ以外へ復旧する場合や、バックアップから元の保存先を確認できない場合は、元のDBと根拠を保全し、保守担当と手順を確認してください。

## 既存のMapを使う方へ

今使っているMapフォルダを指定して開きます。ショートカットや起動スクリプトも、今の保存先を指しているか確かめてください。

## 仕組みを育てる人へ

[HANDOVER.md](HANDOVER.md)に設計・プログラムの配置・拡張時の確認事項があります。終了したWorkを戻す操作、関係変更、複雑な残務の処遇など、ここで案内していない操作を追加する場合は、そちらを参照してください。

この版は1台のWindows PCで使います。会社で使う場合の起動・利用条件は会社側で確認してください。全社導入の適合確認はしていません。利用後に作られる実Work・根拠・バックアップを公開リポジトリへコミットしないでください。

動作確認は `python -X utf8 check.py`で行えます。使い捨ての写しで画面の起動と操作を確認するため、利用中のMapには触れません。初めて試す時は新しいフォルダを使います。

## ライセンス

コード等は[MIT](LICENSE)、説明文書は[CC BY 4.0](LICENSE-DOCS.md)です。適用範囲と著作者表示は[NOTICE](NOTICE.md)、初期版の到達範囲は[リリースノート](RELEASE_NOTES.md)を参照してください。

## 共用プログラムを更新する時

共用設定済みのプログラムが更新中なら、操作を始めずに案内を返します。更新の完了後に開き直してください。復旧が必要と表示された場合は、保守担当の確認を待ちます。初回の共用設定・更新・復元は、保守担当向けの[手順](PROGRAM_MAINTENANCE.md)にまとめています。
