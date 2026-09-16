// 看板前端冒烟测试: 用假 DOM 在 Node 中真实执行 index.html 的脚本并调用渲染函数
// 用法: node ui_smoke_test.js
//   - 脚本自 index.html 提取, 响应数据取自运行中的服务(8000); 服务未运行时回退 _ui_fixture.json
// 覆盖: ①定盘卡片 ②实时卡片 ③无定盘兼容 ④分时点击读数(涨幅%) ⑤9:25-9:30窗口 ⑥开盘后双区
//       ⑦分时量柱 ⑧日K(蜡烛+均线+量柱) ⑨日K点击命中 ⑩视图切换命中口径 ⑪分时右轴涨跌幅标注
//       ⑫右轴标注像素级几何校验(不越界/不压量区)
const fs = require('fs');
const path = require('path');
const DIR = __dirname;

function extractScript(){
  const html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
  const m = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
  if (!m.length) throw new Error('index.html 中未找到 <script>');
  return m.map(x=>x[1]).join('\n;\n');
}
async function loadFixture(){
  try {
    const r = await fetch('http://127.0.0.1:8000/api/bid_watch');
    const j = await r.json();
    console.log('[fixture] 取自运行中的服务 8000');
    return j;
  } catch (e) {
    const p = path.join(DIR, '_ui_fixture.json');
    if (!fs.existsSync(p)) throw new Error('服务未运行且无 _ui_fixture.json: ' + e.message);
    console.log('[fixture] 服务未运行, 回退 _ui_fixture.json');
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  }
}

// 绘制调用计数: 用于断言「量柱/蜡烛确实画了」, 而不是只看函数没抛错
// texts 记录每次 fillText 的文本与当时 fillStyle, 用于断言刻度标注内容/着色
const draw = { clear:0, fillRect:0, strokeRect:0, fillText:0, stroke:0, arc:0, texts:[] };
function drawReset(){ Object.keys(draw).forEach(k=>draw[k]=0); draw.texts = []; }
const ctx2d = {
  clearRect(){ draw.clear++; }, fillRect(){ draw.fillRect++; }, strokeRect(){ draw.strokeRect++; },
  beginPath(){}, moveTo(){}, lineTo(){}, stroke(){ draw.stroke++; },
  setLineDash(){}, arc(){ draw.arc++; }, fill(){}, save(){}, restore(){}, closePath(){},
  fillText(t,x,y){ draw.fillText++; draw.texts.push({t:String(t), c:String(this.fillStyle),
    x:x, y:y, a:String(this.textAlign), b:String(this.textBaseline)}); },
  measureText(t){ return {width: String(t).length*6}; },
  fillStyle:'', strokeStyle:'', lineWidth:1, font:'', textAlign:'', textBaseline:''
};
const cache = {};
function mk(sel){
  const el = {
    sel, style:{}, dataset:{}, innerHTML:'', textContent:'', value:'', checked:false,
    classList:{ add(){}, remove(){}, contains(){return false}, toggle(){} },
    _h:{}, addEventListener(type, fn){ (el._h[type] = el._h[type] || []).push(fn); },
    appendChild(){}, setAttribute(){}, removeAttribute(){},
    querySelector:()=>mk('sub'), querySelectorAll:()=>[], closest:()=>null,
    getContext:()=>ctx2d, focus(){}, click(){}, remove(){}, children:[], firstChild:null,
    width:760, height:440,
    getBoundingClientRect:()=>({left:0, top:0, width:760, height:440})
  };
  return el;
}
function fire(el, type, ev){
  const list = (el._h && el._h[type]) || [];
  if(!list.length) return false;
  list.forEach(fn=>fn(ev));
  return true;
}
const document = {
  querySelector:(sel)=> cache[sel] || (cache[sel]=mk(sel)),
  querySelectorAll:()=>[],
  getElementById:(id)=> cache['#'+id] || (cache['#'+id]=mk('#'+id)),
  createElement:(t)=>mk(t), addEventListener(){},
  body: mk('body'), documentElement: mk('html'), cookie:''
};

let fail = 0;
function ck(label, cond, extra){
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + label + (cond ? '' : '   <<< ' + (extra||'')));
  if(!cond) fail++;
}

(async ()=>{
  const src = extractScript();
  const fixture = await loadFixture();
  const H = { fx: fixture };            // 可切换的接口响应

  const G = {
    document, window:{}, console,
    fetch: async()=>({ ok:true, status:200, json:async()=>H.fx }),
    localStorage:{ getItem:()=>null, setItem(){}, removeItem(){} },
    setInterval:()=>0, clearInterval(){}, setTimeout:()=>0, clearTimeout(){},
    alert(){}, navigator:{userAgent:'node'}, performance:{now:()=>Date.now()},
    location:{hash:'',search:'',href:'http://127.0.0.1:8000/'},
    Date, Math, JSON, Number, String, Boolean, Array, Object, Promise, RegExp, Error,
    Set, Map, WeakMap, Intl, encodeURIComponent, decodeURIComponent,
    parseInt, parseFloat, isNaN, isFinite
  };
  const names = Object.keys(G);
  const ret = ';return {renderPickCards, renderBidWatch, drawTrends, showTrendReadout,'
            + ' paintTrends, drawKline, paintKline, showKlineReadout, chartGeom, fmtVol, maSeries,'
            + ' setTr:(v)=>{_tr=v;}, getTr:()=>_tr,'
            + ' setView:(v)=>{_tview=v;}, getView:()=>_tview, getKl:()=>_kl};';
  const api = new Function(...names, src + ret)(...names.map(n=>G[n]));

  const j = fixture, d = j.data, fz = d.frozen;
  console.log('== 数据前提 ==');
  console.log('  phase=' + j.phase + ' freeze_window=' + d.freeze_window
              + ' picks_n=' + d.picks_n + ' frozen=' + (fz ? fz.frozen_at : 'null'));
  if(!fz){ console.log('  [SKIP] 今日无定盘快照, 跳过定盘相关用例'); }

  if(fz){
    console.log('\n== ① 定盘卡片(全天保留) ==');
    const htmlF = api.renderPickCards(fz.picks, j, {frozen:true, modeName:fz.mode_name,
      frozenAt:fz.frozen_at, basisName:fz.basis_name});
    ck('含「竞价定盘 TOP3」标题', htmlF.indexOf('竞价定盘 TOP3') >= 0);
    ck('含「全天保留」标签', htmlF.indexOf('全天保留') >= 0);
    ck('含定格时刻', htmlF.indexOf(fz.frozen_at) >= 0, fz.frozen_at);
    ck('含定盘口径说明', htmlF.indexOf('开盘缺口') >= 0);
    ck('含「定盘缺口」字段', htmlF.indexOf('定盘缺口') >= 0);
    ck('含定格后现价', htmlF.indexOf('现价') >= 0);
    ck('无未替换/未定义占位', htmlF.indexOf('undefined') < 0 && htmlF.indexOf('[object') < 0);
    ck('卡片数=3', (htmlF.match(/pick-card clickable/g)||[]).length === 3,
       (htmlF.match(/pick-card clickable/g)||[]).length);
    ck('定盘卡片含买点门控标注', htmlF.indexOf('买点·') >= 0);
    ck('买点标签为 可买/不买/待定 之一', /可买|不买|待定/.test(htmlF));
    ck('买点档位来自实证分档', /黄金买点|观望|放弃|买不进|高风险/.test(htmlF));
  }

  console.log('\n== ② 实时卡片(开盘后刷新) ==');
  const refCodes = fz ? new Set(fz.picks.map(x=>x.code)) : null;
  const htmlL = api.renderPickCards(d.picks, j, {live:true, hasFrozen:!!fz, refCodes});
  ck('含「开盘后实时精选 TOP3」标题', htmlL.indexOf('开盘后实时精选 TOP3') >= 0);
  ck('含「延续/新进」标注', htmlL.indexOf('延续') >= 0 || htmlL.indexOf('新进') >= 0);
  ck('显示「明早怎么买」纪律行', htmlL.indexOf('明早怎么买') >= 0);
  ck('实时卡片不显示定盘缺口字段', htmlL.indexOf('定盘缺口') < 0);
  ck('纪律行已用新买点口径(低开 -4%~-1%)', htmlL.indexOf('低开 -4%~-1%') >= 0);
  ck('纪律行含「小高开 0~+5% 观望」提示', htmlL.indexOf('小高开 0~+5% 观望') >= 0);
  ck('纪律行已废弃旧口径(0~4% 才买)', htmlL.indexOf('平开~小高开(0~4%)') < 0);

  console.log('\n== ③ 无定盘时的兼容(盘前/首次) ==');
  const htmlN = api.renderPickCards(d.picks, j, {live:true, hasFrozen:false, refCodes:null});
  ck('回落为「赚钱效应 TOP3」', htmlN.indexOf('赚钱效应 TOP3') >= 0);

  console.log('\n== ④ 分时点击读数(涨幅%) ==');
  const pts = [
    {t:'2026-09-14 09:30', price:14.51, avg:14.40},
    {t:'2026-09-14 10:30', price:15.27, avg:14.90},
    {t:'2026-09-14 13:05', price:13.50, avg:14.60}
  ];
  api.drawTrends({code:'000993', name:'闽东电力', preClose:13.88, points:pts}, '闽东电力');
  ck('标题含昨收', String(cache['#trendTitle'].innerHTML).indexOf('昨收 13.88') >= 0,
     cache['#trendTitle'].innerHTML);
  ck('画布已显示', cache['#trendCanvas'].style.display === 'block');
  api.showTrendReadout(1);
  let ro = String(cache['#trendReadout'].innerHTML);
  const upChg = ((15.27-13.88)/13.88*100).toFixed(2);
  ck('涨点读数含时间 10:30', ro.indexOf('10:30') >= 0, ro);
  ck('涨点涨幅% = +' + upChg + '%', ro.indexOf('+'+upChg+'%') >= 0, ro);
  ck('涨点样式为红(up)', ro.indexOf('class="up"') >= 0);
  ck('读数区已显示', cache['#trendReadout'].style.display === 'flex');
  api.showTrendReadout(2);
  ro = String(cache['#trendReadout'].innerHTML);
  const dnChg = ((13.50-13.88)/13.88*100).toFixed(2);
  ck('跌点涨幅% = ' + dnChg + '%', ro.indexOf(dnChg+'%') >= 0, ro);
  ck('跌点样式为绿(down)', ro.indexOf('class="down"') >= 0);

  if(fz){
    console.log('\n== ⑤ 界面组合: 9:25-9:30 定盘窗口(只呈现定盘名单, 暂停刷新) ==');
    H.fx = Object.assign({}, fixture, {freeze_window:true,
            data: Object.assign({}, d, {freeze_window:true})});
    await api.renderBidWatch();
    let html = String(cache['#bidBody'].innerHTML);
    ck('呈现定盘名单', html.indexOf('竞价定盘 TOP3') >= 0);
    ck('不呈现实时精选区', html.indexOf('开盘后实时精选 TOP3') < 0);
    ck('给出 9:25-9:30 窗口提示', html.indexOf('9:25-9:30') >= 0);
    ck('完整名单表仍在(折叠)', html.indexOf('展开全部') >= 0);
    ck('顶部标注定盘已锁定', String(cache['#bidCnt'].innerHTML).indexOf('已锁定') >= 0,
       cache['#bidCnt'].innerHTML);

    console.log('\n== ⑥ 界面组合: 开盘后(定盘保留 + 实时刷新) ==');
    H.fx = fixture;
    await api.renderBidWatch();
    html = String(cache['#bidBody'].innerHTML);
    ck('定盘区继续保留', html.indexOf('竞价定盘 TOP3') >= 0);
    ck('实时精选区出现', html.indexOf('开盘后实时精选 TOP3') >= 0);
    ck('定盘区排在实时区之前', html.indexOf('竞价定盘 TOP3') < html.indexOf('开盘后实时精选 TOP3'));
    ck('不再显示窗口提示', html.indexOf('9:25-9:30 竞价定盘窗口') < 0);
  }

  console.log('\n== ⑦ 分时量柱(分钟级) ==');
  const pts2 = [];
  for(let i=0;i<30;i++){
    const px = 14.50 + i*0.02;
    pts2.push({t:'2026-09-14 09:'+String(30+i).padStart(2,'0'),
               o:14.50, price:px, avg:14.60, vol:(i===5? 200000 : 1000+i*37)});
  }
  drawReset();
  api.drawTrends({code:'000993', name:'闽东电力', preClose:13.88, points:pts2}, '闽东电力');
  ck('标题标注「分时」', String(cache['#trendTitle'].innerHTML).indexOf('分时') >= 0,
     cache['#trendTitle'].innerHTML);
  ck('量柱已绘制(fillRect >= 点数)', draw.fillRect >= pts2.length,
     draw.fillRect + ' vs ' + pts2.length);
  api.showTrendReadout(5);
  ck('分时读数含成交量(20.00万手)',
     String(cache['#trendReadout'].innerHTML).indexOf('20.00万手') >= 0,
     cache['#trendReadout'].innerHTML);

  console.log('\n== ⑧ 日K: 蜡烛 + MA5/10/20 + 量柱 ==');
  const bars = [];
  for(let i=0;i<60;i++){
    const base = 10 + Math.sin(i/4)*1.2 + i*0.05;
    const o = base, c = base + (i%3===0? -0.25 : 0.30);
    bars.push({d:'2026-'+String(6+Math.floor(i/22)).padStart(2,'0')+'-'
                 +String(1+i%22).padStart(2,'0'),
               o:o, c:c, h:Math.max(o,c)+0.2, l:Math.min(o,c)-0.2,
               v:100000+i*1000, amt:null, chg:(i? 0.5:null)});
  }
  const klFix = {ok:true, code:'000993', name:'闽东电力', days:bars.length,
                 preClose:bars[bars.length-2].c, bars:bars};
  api.setView('kline');
  drawReset();
  api.drawKline(klFix, '闽东电力');
  ck('标题含「日K 60根」', String(cache['#trendTitle'].innerHTML).indexOf('日K 60根') >= 0,
     cache['#trendTitle'].innerHTML);
  ck('画布已显示', cache['#trendCanvas'].style.display === 'block');
  ck('蜡烛实体+量柱已绘制(fillRect >= 2×根数)', draw.fillRect >= bars.length*2,
     draw.fillRect + ' vs ' + bars.length*2);
  ck('影线+3条均线已绘制(stroke >= 根数+3)', draw.stroke >= bars.length+3, draw.stroke);
  ck('网格/量轴/均线图例文本已绘制', draw.fillText >= 7, draw.fillText);
  api.showKlineReadout(59);
  const roK = String(cache['#trendReadout'].innerHTML);
  ck('日K读数含日期', roK.indexOf(bars[59].d) >= 0, roK);
  ck('日K读数含 开/高/低/收',
     roK.indexOf('开 ')>=0 && roK.indexOf('高 ')>=0 && roK.indexOf('低 ')>=0 && roK.indexOf('收 ')>=0, roK);
  ck('日K读数含涨跌幅% 与 量(手)', roK.indexOf('%')>=0 && roK.indexOf('手')>=0, roK);
  ck('日K读数无 undefined/NaN', roK.indexOf('undefined')<0 && roK.indexOf('NaN')<0, roK);

  console.log('\n== ⑨ 日K 点击命中(按根定位, 再点取消) ==');
  const cvEl = cache['#trendCanvas'];
  const Gk = api.chartGeom(760, 440);
  const bwK = (760-Gk.padL-Gk.padR)/bars.length;
  const target = 20;
  const hitX = Gk.padL + bwK*(target+0.5);
  ck('已注册画布 click 处理器', fire(cvEl, 'click', {clientX:hitX, clientY:200}));
  ck('命中第 '+(target+1)+' 根', api.getKl() && api.getKl().mark === target,
     api.getKl() ? String(api.getKl().mark) : 'null');
  ck('命中读数指向该日', String(cache['#trendReadout'].innerHTML).indexOf(bars[target].d) >= 0,
     cache['#trendReadout'].innerHTML);
  fire(cvEl, 'click', {clientX:hitX, clientY:200});
  ck('再点同一点取消定位', api.getKl().mark === null, String(api.getKl().mark));

  console.log('\n== ⑩ 视图切回分时: 命中判定走分时口径 ==');
  api.setView('trend');
  const nT = api.getTr().pts.length;
  const xT = Gk.padL + (760-Gk.padL-Gk.padR)*(3/(nT-1));
  fire(cvEl, 'click', {clientX:xT, clientY:150});
  ck('分时视图命中第 4 点', api.getTr().mark === 3, String(api.getTr().mark));
  ck('分时量与日K量互不干扰', api.getKl().mark === null);

  console.log('\n== ⑪ 分时右轴标注「价格 + 涨跌幅%」 ==');
  drawReset();
  api.drawTrends({code:'000993', name:'闽东电力', preClose:13.88, points:pts2}, '闽东电力');
  const tTxt = draw.texts.map(x=>x.t);
  const pcts = tTxt.filter(t=>/%$/.test(t));
  ck('绘制 4 条涨跌幅刻度', pcts.length === 4, pcts.join(' '));
  ck('价格刻度仍保留(4 条)', tTxt.filter(t=>/^\d+\.\d{2}$/.test(t)).length === 4, tTxt.join(' '));
  ck('涨跌幅格式为 +x.xx% / -x.xx%', pcts.every(t=>/^[+-]?\d+\.\d{2}%$/.test(t)), pcts.join(' '));
  ck('含相对昨收为正的刻度', pcts.some(t=>/^\+\d/.test(t)), pcts.join(' '));
  ck('含相对昨收为负的刻度', pcts.some(t=>/^-/.test(t)), pcts.join(' '));
  ck('无 "-0.00%" 伪负零', pcts.indexOf('-0.00%') < 0, pcts.join(' '));
  const redP = draw.texts.filter(x=>/%$/.test(x.t) && x.c === '#f23645').map(x=>x.t);
  const grnP = draw.texts.filter(x=>/%$/.test(x.t) && x.c === '#2bbf6a').map(x=>x.t);
  ck('高于昨收的刻度着红', redP.length >= 1, redP.join(' '));
  ck('低于昨收的刻度着绿', grnP.length >= 1, grnP.join(' '));
  ck('价格刻度着灰(不与涨跌幅混色)',
     draw.texts.filter(x=>/^\d+\.\d{2}$/.test(x.t) && x.c === '#6f7d97').length === 4,
     draw.texts.map(x=>x.t+':'+x.c).join(' '));

  drawReset();
  api.drawKline(klFix, '闽东电力');
  ck('日K 右轴不标注涨跌幅(维持单行价格)',
     draw.texts.filter(x=>/%$/.test(x.t)).length === 0,
     draw.texts.map(x=>x.t).join(' '));

  console.log('\n== ⑫ 右轴标注像素级几何校验(不越界 / 不压量区) ==');
  // 取实测标的做几何校验: 苏州天脉 301626, 昨收 299.00, 盘中 297.33~321.55
  const realPts = [
    {t:'2026-09-16 09:30', price:305.00, avg:305.00, o:305.00, vol:1000},
    {t:'2026-09-16 10:30', price:297.33, avg:302.10, o:303.00, vol:1200},
    {t:'2026-09-16 11:30', price:312.00, avg:305.50, o:308.00, vol:900},
    {t:'2026-09-16 14:00', price:321.55, avg:310.20, o:316.00, vol:1500}
  ];
  drawReset();
  api.drawTrends({code:'301626', name:'苏州天脉', preClose:299.00, points:realPts}, '苏州天脉');
  const Gp = api.chartGeom(760, 440);
  const tw = t => String(t).length * 6;            // 与 mock measureText 同口径
  const isPx = t => /^\d+\.\d{2}$/.test(t), isPc = t => /^[+-]?\d+\.\d{2}%$/.test(t);
  const axisAll = draw.texts.filter(x => isPx(x.t) || isPc(x.t));
  const clip = [], overVol = [];
  axisAll.forEach(x => {
    const left = (x.a === 'right') ? x.x - tw(x.t) : x.x;
    const right = left + tw(x.t);
    const top = (x.b === 'bottom') ? x.y - 10 : ((x.b === 'top') ? x.y : x.y - 5);
    const bot = top + 10;
    if(left < 0 || right > 760 || top < 0 || bot > 440) clip.push(x.t + '[' + left.toFixed(0) + ',' + top.toFixed(0) + ']');
    if(bot > Gp.volTop && top < Gp.volBot) overVol.push(x.t);
  });
  ck('轴标注 4 价格 + 4 涨跌幅', axisAll.length === 8,
     'px=' + axisAll.filter(x=>isPx(x.t)).length + ' pct=' + axisAll.filter(x=>isPc(x.t)).length);
  ck('全部落在画布内(无裁切)', clip.length === 0, clip.join(' '));
  ck('不与成交量区重叠', overVol.length === 0, overVol.join(' '));
  ck('涨跌幅文本右边界留有余量(<=760)',
     Math.max.apply(null, draw.texts.filter(x=>isPc(x.t)).map(x=>x.x + tw(x.t))) <= 760,
     String(Math.max.apply(null, draw.texts.filter(x=>isPc(x.t)).map(x=>x.x + tw(x.t)))));
  ck('涨跌幅与价格同一水平位(价格在上)',
     draw.texts.filter(x=>isPc(x.t)).every(pc => {
       const px = draw.texts.find(x=>isPx(x.t) && Math.abs(x.x - pc.x) < 0.01
                                      && Math.abs(pc.y - x.y - 3) < 0.01);
       return !!px;
     }), '需成对出现');

  console.log('\n' + (fail ? '[FAIL] 失败 ' + fail + ' 项' : '[OK] 全部通过'));
  process.exit(fail ? 1 : 0);
})().catch(e=>{ console.error('[ERROR]', e); process.exit(2); });
