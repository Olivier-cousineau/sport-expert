const fs = require('fs');
const path = require('path');

const DEFAULT_INPUT = path.join(__dirname, '..', 'sportsexperts-2025-12-27 (2).json');
const DEFAULT_OUTPUT = path.join(__dirname, '..', 'data', 'online.normalized.json');

function parseArgs(argv) {
  const args = {
    input: DEFAULT_INPUT,
    output: DEFAULT_OUTPUT,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const value = argv[i];
    if (value === '--input' && argv[i + 1]) {
      args.input = argv[i + 1];
      i += 1;
      continue;
    }
    if (value === '--output' && argv[i + 1]) {
      args.output = argv[i + 1];
      i += 1;
    }
  }

  return args;
}

function parsePrice(text) {
  if (!text || typeof text !== 'string') {
    return null;
  }

  const normalized = text
    .replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/\$/g, '')
    .replace(/\s/g, '')
    .replace(',', '.');

  const value = Number.parseFloat(normalized);
  return Number.isFinite(value) ? value : null;
}

function normalizeItem(item) {
  const saleText = item['font-size-sm'] || null;
  const regularText = item['font-size-sm (2)'] || null;

  return {
    url: item['product-tile-media href'] || null,
    image_url: item['img-fit src'] || null,
    brand: item['product-tile-brand'] || null,
    title: item['product-tile-title'] || null,
    price_sale: parsePrice(saleText),
    price_regular: parsePrice(regularText),
    price_sale_text: saleText,
    price_regular_text: regularText,
    discount_label: item['discount-label'] || null,
  };
}

function normalizeData(raw) {
  const items = Array.isArray(raw) ? raw : [];

  return {
    source: 'sportsexperts',
    generated_at: new Date().toISOString(),
    items: items.map(normalizeItem),
  };
}

function main() {
  const { input, output } = parseArgs(process.argv);

  const rawContent = fs.readFileSync(input, 'utf8');
  const rawData = JSON.parse(rawContent);
  const normalized = normalizeData(rawData);

  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(normalized, null, 2)}\n`, 'utf8');

  console.log(`Normalized ${normalized.items.length} items to ${output}`);
}

main();
