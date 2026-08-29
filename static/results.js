// Live recompute for the results page's purchase price / cost/sqft /
// margin inputs. Mirrors services/rebuild_calc.py's calculate_rebuild_deal()
// exactly — keep the two in sync if the formula ever changes. Deliberately
// has no network calls: propertySqft and marketValueEstimate are fixed per
// analysis and embedded in the page already; purchase price, cost/sqft,
// and margin are all live inputs the user can adjust.
document.addEventListener('DOMContentLoaded', () => {
  const dataEl = document.getElementById('analysis-data');
  const purchasePriceInput = document.getElementById('purchase_price');
  const costInput = document.getElementById('cost_per_sqft');
  const marginInput = document.getElementById('profit_margin_pct');
  if (!dataEl || !purchasePriceInput || !costInput || !marginInput) return;

  const fixed = JSON.parse(dataEl.textContent);

  const verdictSection = document.getElementById('verdict');
  const verdictLabel = document.getElementById('verdict-label');
  const achievableMarginEl = document.getElementById('achievable-margin');
  const buildCostEl = document.getElementById('build-cost');
  const totalCostEl = document.getElementById('total-cost');
  const requiredSalePriceEl = document.getElementById('required-sale-price');

  const currency = (value) =>
    value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

  function calculateRebuildDeal({ purchasePrice, propertySqft, costPerSqft, profitMarginPct, marketValueEstimate }) {
    const profitMargin = profitMarginPct / 100;
    const buildCost = costPerSqft * propertySqft;
    const totalCost = purchasePrice + buildCost;
    const requiredSalePrice = totalCost * (1 + profitMargin);
    const achievableMargin = totalCost ? (marketValueEstimate - totalCost) / totalCost : 0;
    const isWorthIt = marketValueEstimate >= requiredSalePrice;
    return { buildCost, totalCost, requiredSalePrice, achievableMargin, isWorthIt };
  }

  function recompute() {
    const purchasePrice = parseFloat(purchasePriceInput.value) || 0;
    const costPerSqft = parseFloat(costInput.value) || 0;
    const profitMarginPct = parseFloat(marginInput.value) || 0;

    const deal = calculateRebuildDeal({
      purchasePrice,
      propertySqft: fixed.propertySqft,
      costPerSqft,
      profitMarginPct,
      marketValueEstimate: fixed.marketValueEstimate,
    });

    buildCostEl.textContent = currency(deal.buildCost);
    totalCostEl.textContent = currency(deal.totalCost);
    requiredSalePriceEl.textContent = currency(deal.requiredSalePrice);
    achievableMarginEl.textContent = (deal.achievableMargin * 100).toFixed(1) + '%';
    verdictLabel.textContent = deal.isWorthIt ? 'Worth It' : 'Not Worth It';
    verdictSection.classList.toggle('worth-it', deal.isWorthIt);
    verdictSection.classList.toggle('not-worth-it', !deal.isWorthIt);
  }

  purchasePriceInput.addEventListener('input', recompute);
  costInput.addEventListener('input', recompute);
  marginInput.addEventListener('input', recompute);
  recompute();
});
