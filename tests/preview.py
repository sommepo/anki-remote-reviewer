"""Generate an offline browser fixture. Does not run a server or use real cards."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
html = html.replace('<link rel="stylesheet" href="/app.css" />', '<style>' + (ROOT / 'web/app.css').read_text(encoding='utf-8') + '</style>')
fixture = '''<script>
let side = 'idle', revision = 1;
window.fetch = async (url, options = {}) => {
 const body = options.body ? JSON.parse(options.body) : {};
 if (url.endsWith('/start')) {side = 'question'; revision++;}
 if (url.endsWith('/reveal')) {side = 'answer'; revision++;}
 if (url.endsWith('/answer')) {side = 'question'; revision++;}
 if (url.endsWith('/stop')) {side = 'idle'; revision++;}
 const data = {version:'0.2.0-beta.1',status:side,revision:String(revision),decks:['Japanese','French'],deck:'Japanese',cardId:42,canUndo:true,showIntervals:true,
 questionHtml:'<div class="word">思い出</div><p>大切な<span class="highlight">思い出</span>です。</p>',
 answerHtml:'<div class="word"><ruby>思い出<rt>おもいで</rt></ruby></div><p>A memory; a recollection.</p><div class="sample-picture">思い出</div><p>大切な<span class="highlight">思い出</span>です。</p>',
 css:'.card{text-align:center;font-size:24px}.word{font-size:42px;margin:20px}.highlight{color:var(--red);background:#171717;padding:3px;border-radius:3px}.sample-picture{background:#3b4846;height:240px;display:grid;place-items:center;margin:20px auto;max-width:540px;border-radius:4px}',
 answerAudio:[],buttons:[{ease:1,label:'Again',interval:'1m'},{ease:2,label:'Hard',interval:'1d'},{ease:3,label:'Good',interval:'3d'},{ease:4,label:'Easy',interval:'5d'}]};
 return {ok:true,json:async()=>data};
};
</script>'''
html = html.replace('<script src="/app.js"></script>', fixture + '<script>' + (ROOT / 'web/app.js').read_text(encoding='utf-8') + '</script>')
output = ROOT / 'tests/preview.html'
output.write_text(html, encoding='utf-8')
print(output)
