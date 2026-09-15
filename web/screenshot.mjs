// Screenshot the running page, for checking a layout change without eyes on
// a browser. Usage: node screenshot.mjs [out.png] [url]
import { chromium } from 'playwright';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
await page.goto(process.argv[3] ?? 'http://localhost:8040', {
  waitUntil: 'networkidle',
});
await page.waitForSelector('h1');
await page.waitForTimeout(1500); // let a couple of poll/level-stream ticks land
await page.screenshot({ path: process.argv[2] ?? 'screenshot.png', fullPage: true });
await browser.close();
