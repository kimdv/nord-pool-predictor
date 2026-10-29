export interface SlotBreakdown {
  hour: number;
  minute: number;
  ts: string;
  spot_price: number | null;
  spot_source: "actual" | "forecast" | "missing";
  grid_tariff: number;
  system_tariff: number;
  transmission_tariff: number;
  grid_loss_tariff: number;
  electricity_tax: number;
  total_ex_vat: number | null;
  vat: number | null;
  total_incl_vat: number | null;
}

export function buildPriceBreakdownSeries(
  slots: SlotBreakdown[],
  disabled: ReadonlySet<string>,
  activeSlotIndex: number,
) {
  return slots.map((slot, index) => {
    const identity = {
      ts: slot.ts,
      isCurrent: index === activeSlotIndex,
      spot_source: slot.spot_source,
    };
    if (slot.spot_price === null) {
      return {
        ...identity,
        spot_price: null, grid_tariff: null, transport: null,
        electricity_tax: null, vat: null, total: null,
      };
    }
    const spot = disabled.has("spot_price") ? 0 : slot.spot_price;
    const grid = disabled.has("grid_tariff") ? 0 : slot.grid_tariff;
    const transport = disabled.has("transport") ? 0 :
      slot.system_tariff + slot.transmission_tariff + slot.grid_loss_tariff;
    const tax = disabled.has("electricity_tax") ? 0 : slot.electricity_tax;
    const subtotal = spot + grid + transport + tax;
    const vat = disabled.has("vat") ? 0 : subtotal * 0.25;
    return {
      ...identity,
      spot_price: spot, grid_tariff: grid, transport,
      electricity_tax: tax, vat, total: subtotal + vat,
    };
  });
}
