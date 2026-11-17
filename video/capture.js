const { chromium } = require('playwright');
const V = process.argv[2], API = 'http://localhost:8099', TOK = 'localtoken';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const req = p.context().request;
  const act = (body) => req.post(API + '/demo/api/presenter', { data: { token: TOK, ...body } });
  const status = async () => (await (await req.get(API + '/demo/api/status')).json()).merchants
      .map(m => ({ name: m.name, age: m.freshness.age_s, label: m.freshness.label }));
  const shot = async (n) => { await sleep(700); await p.screenshot({ path: `${V}/frames/${n}.jpg`, type: 'jpeg', quality: 92 }); };
  await act({ action: 'reset' });
  await p.goto('file://' + V + '/stage.html'); await sleep(2500);
  const app = p.frameLocator('#app');

  await p.evaluate(() => card(`<div class="mark">${MARK}<span>TrueMenu</span></div>
    <h1 class="big g">Menus go stale.<br>Agents don't know.</h1>
    <p class="sub">TrueMenu gives AI ordering agents the restaurant's own prices and hours, says how fresh they are, and signs a quote any agent can check before it pays.</p>`));
  await shot('s1');

  await p.evaluate(() => card(`<div class="kicker">The problem</div>
    <h1 class="big g" style="font-size:96px">When a restaurant's data goes stale, an agent reading its menu rarely notices.</h1>
    <div class="stats"><div class="stat"><div class="n" style="color:#1E7A4C">6 of 6</div><div class="l">stale cases declined, agent using TrueMenu</div></div>
    <div class="stat"><div class="n" style="color:#8A929C">1 of 6</div><div class="l">stale cases declined, same model reading the raw menu</div></div></div>
    <div class="foot">HW7 benchmark: same model, two stale-data cases, 3 runs each</div>`));
  await shot('s2');

  await p.evaluate(() => demo('1 · The ask', "A visitor's agent asks for lunch nearby",
    'No pork, low carb, under $20, in Kendall Square. Real menus from three Kendall Square restaurants.'));
  await app.locator('#diet [data-v="low_carb"]').click();
  await shot('s3');

  await app.locator('#find').click(); await app.locator('#answer:not([hidden])').waitFor(); await sleep(3200);
  let m = await status();
  await p.evaluate((m) => demo('2 · The answer', 'TrueMenu answers, shows how fresh the data is, and signs the price',
    'Checked all 27 dishes; 6 fit the rules. The quote is signed and valid for 15 minutes.', feeds(m)), m);
  await shot('s4');

  await act({ action: 'raise_price', item_id: 'cava-greek-salad' });
  await p.evaluate(() => demo('3 · The price changes', 'The restaurant raises the price',
    "CAVA's system now reports $14.25. The visitor's quote still says $12.25.",
    log([['red', 'Restaurant changed Greek Salad to $14.25']])));
  await shot('s5a');

  await app.locator('#pay').click(); await app.locator('#payresult:not([hidden])').waitFor(); await sleep(600);
  await p.evaluate(() => demo('3 · The price changes', 'Before paying, the agent checks the quote: blocked',
    'TrueMenu sees the price changed after the quote was signed. The agent does not pay.',
    log([['red', 'Restaurant changed Greek Salad to $14.25'], ['red', 'Payment blocked: price changed after the quote']])));
  await app.locator('#payresult').scrollIntoViewIfNeeded();
  await shot('s5b');

  await act({ action: 'reset' }); await act({ action: 'pause_all' });
  await sleep(33000);
  m = await status();
  await app.locator('#restart').click(); await sleep(300);
  await p.evaluate((m) => demo('4 · The data goes stale', 'Now the restaurants stop syncing',
    'No update for more than 30 seconds, twice the normal interval: TrueMenu marks the data stale.', feeds(m)), m);
  await shot('s6a');

  await app.locator('#find').click(); await app.locator('#answer:not([hidden])').waitFor(); await sleep(800);
  await p.evaluate((m) => demo('4 · The data goes stale', 'TrueMenu refuses to sign a price it cannot stand behind',
    'No fresh data, no quote. The agent tells the user and does not pay.', feeds(m)), m);
  await shot('s6b');
  await act({ action: 'reset' });

  await p.evaluate(() => card(`<div class="kicker">Tested from the docs alone</div>
    <h1 class="big g" style="font-size:96px">Two different agents used TrueMenu with only the docs and a key.</h1>
    <div class="stats"><div class="stat"><div class="n">4 of 4</div><div class="l">runs by our agent met every check we set before the test</div></div>
    <div class="stat"><div class="n">1 of 1</div><div class="l">run by another team's agent met them too, and refused to pay a day early on a 15-minute quote</div></div></div>`));
  await shot('s7');

  await p.evaluate(() => card(`<div class="mark">${MARK}<span>TrueMenu</span></div>
    <h1 class="big g">Verified menus for<br>AI ordering agents</h1>
    <p class="sub">Next: quotes that hold until meal time, and live feeds from restaurants on Nugttah's ordering platform in Riyadh.</p>
    <div class="foot">Shahaleel Al Yousef · MIT AI Studio Demo Day 2026 · github.com/IntelCodes/truemenu</div>`));
  await shot('s8');
  await b.close();
})();
