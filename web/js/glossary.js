// 解説文に出てくる囲碁用語に、その場で意味を出せるようにする。
//
// 解説が分かりにくい原因の一つが、説明の中で当たり前のように専門用語を
// 使っていること。用語を消すと逆に説明が長く回りくどくなるので、
// 用語はそのまま使い、押せば意味が出るようにする。
//
// 語釈は go_review/tagging.py の TAG_DESCRIPTIONS と重なるものは
// 同じ言い回しに揃えてある。画面ごとに違う説明が出ると混乱するため。

const GLOSSARY = {
  // 盤の上の基本
  呼吸点: '石のとなりにある空いた交点。ここが全部埋まると石は取られる。',
  ダメ: '地にならない、石と石の間に残った空き点のこと。呼吸点と同じ意味で使うこともある。',
  欠け眼: '一見すると眼に見えても、まわりの石が取られると眼でなくなる形。',
  中手: '相手の眼の中に石を置いて、眼を 2 つ作らせないようにする手筋。',
  急所: 'そこに打つかどうかで、石の生き死にや形が決まってしまう一点。',
  死活: '石の一団が生きるか死ぬか、ということ。',
  眼が2つ: '自分の石で完全に囲んだ空き点が 2 か所あること。こうなればその石は取られない。',
  地の見込み: '最後に自分の点になりそうな場所の広さ。目の数で表す。',
  大場: '盤全体で見て、いま打つと一番得が大きい場所。',
  ヨセ: '終盤に、地の境目を決めていく打ち方。',
  先手: '打ったあと相手が応じてくれて、もう一度自分に手番が回ってくること。',
  後手: '打ったあと手番が相手に渡ってしまうこと。',

  // 石の取り方・手筋
  アタリ: 'あと 1 手で石が取られる状態。呼吸点が 1 つしか残っていない。',
  両アタリ: '一手で二か所の石を同時にアタリにすること。相手はどちらか片方しか守れない。',
  シチョウ: '石を斜めに追いかけていくと必ず取れる手筋。読み違えると逆に大きな石を失う。',
  ゲタ: '相手の石を、一手で外へ逃げられない網の形に囲ってしまう手筋。',
  ウッテガエシ: '一度わざと取らせてから、すぐに取り返す手筋。',
  オイオトシ: '相手が自分から動くと、連鎖的に大きく取られてしまう形。',
  攻め合い: 'お互いの石が取られそうな状態で、先に相手の呼吸点を詰めたほうが勝つ競争。',
  切断: '相手の石と石のつながりを断ち切ること。',
  捨て石: 'わざと取らせて、そのぶん別の場所で得をする打ち方。',
  手筋: 'その形で一番よく働く、決まった打ち方。',

  // ミスの型（tagging.py の TAG_DESCRIPTIONS と同じ説明）
  アタリ見落とし: '石が取られる一歩手前（アタリ）に気づかず、そのままにしてしまうこと。',
  切断された: '自分の石同士のつながりを、相手に断ち切られてしまうこと。',
  切断機会の逸失: '相手の石を切り離せるチャンスがあったのに、逃してしまったこと。',
  攻め合い負け: 'お互いの石の生死を懸けた呼吸点の詰め合い（攻め合い）に負けること。',
  眼形不足: '自分の石が生きるために必要な眼の形が足りていないこと。',
  大場放置: '盤面全体で価値の高い場所（大場）を打たず、後回しにしてしまったこと。',
  ヨセ損: '終盤の細かい地の取り合い（ヨセ）で、目数を損したこと。',
  弱い石の放置: '自分の弱い石を補強せず、そのままにしてしまったこと。',
  逃げ一辺倒: '弱い石をただ逃がすことだけを考えて、他の手を検討しなかったこと。',
  追いかけすぎ: '相手の弱い石を必要以上に攻め続けて、他の大きな場所を逃したこと。',
  局所固執: '一か所の攻防にこだわりすぎて、盤面全体を見た判断を誤ったこと。',
  捨て石の判断ミス: '石を捨てるべき場面で捨てなかった、または捨てるべきでない場面で捨ててしまったこと。',
  先手後手の誤り: '手番を渡してもよい手（後手）を打ってしまい、主導権（先手）を失ったこと。',
  ダメ詰めミス: '最後に残る細かい境界線（ダメ）の打ち方で損をしたこと。',
};

// 長い語から先に照合する。「アタリ」を先に当てると「アタリ見落とし」が
// 拾えなくなるため、順序が意味を持つ。
//
// 1 文字の語は載せない。「目」は「6手目」に、「地」は「この地点」に
// 当たってしまい、関係のない場所へ用語の印が付く。日本語には語の
// 区切りが無いので、短い語ほど誤って当たる。
const TERMS = Object.keys(GLOSSARY)
  .filter((t) => t.length >= 2)
  .sort((a, b) => b.length - a.length);

export function describeTerm(term) {
  return GLOSSARY[term] || '';
}

/**
 * 解説文を、用語がタップできる形にして返す。
 *
 * 同じ語が何度も出てくるとリンクだらけになって読めなくなるので、
 * 印を付けるのは各語の最初の 1 回だけにしている。
 */
export function renderExplanation(text) {
  const host = document.createElement('div');
  host.className = 'explanation';
  const note = document.createElement('p');
  note.className = 'term-note';
  note.hidden = true;

  const used = new Set();
  let rest = String(text || '');

  while (rest) {
    let hit = null;
    for (const term of TERMS) {
      if (used.has(term)) continue;
      const at = rest.indexOf(term);
      if (at >= 0 && (hit === null || at < hit.at)) hit = { term, at };
    }
    if (!hit) {
      host.appendChild(document.createTextNode(rest));
      break;
    }
    if (hit.at > 0) host.appendChild(document.createTextNode(rest.slice(0, hit.at)));

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'term';
    button.textContent = hit.term;
    button.addEventListener('click', () => {
      if (!note.hidden && note.dataset.term === hit.term) {
        note.hidden = true;
        return;
      }
      note.dataset.term = hit.term;
      note.textContent = `${hit.term}: ${GLOSSARY[hit.term]}`;
      note.hidden = false;
    });
    host.appendChild(button);

    used.add(hit.term);
    rest = rest.slice(hit.at + hit.term.length);
  }

  host.appendChild(note);
  return host;
}
