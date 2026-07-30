// No chart library: bars over at most ~90 daily points is a small enough
// problem that inline SVG is simpler than a dependency, and it means the
// admin bundle needs nothing beyond Preact.
export interface BarChartProps {
  values: number[];
  labels: string[];
  color?: string;
  height?: number;
  formatValue?: (value: number) => string;
}

export function BarChart({ values, labels, color = "#0f766e", height = 120, formatValue }: BarChartProps) {
  const width = Math.max(values.length * 14, 200);
  const max = Math.max(1, ...values);
  const barWidth = Math.max(2, width / values.length - 2);

  return (
    <svg
      class="admin-chart"
      viewBox={`0 0 ${width} ${height + 20}`}
      width="100%"
      height={height + 20}
      role="img"
      aria-label="daily values"
    >
      {values.map((value, index) => {
        const barHeight = (value / max) * height;
        const x = index * (width / values.length);
        const y = height - barHeight;
        const label = formatValue ? formatValue(value) : String(value);
        return (
          <g key={labels[index] ?? index}>
            <rect
              x={x}
              y={y}
              width={barWidth}
              height={Math.max(barHeight, value > 0 ? 1 : 0)}
              fill={color}
              rx={1.5}
            >
              <title>
                {labels[index]}: {label}
              </title>
            </rect>
          </g>
        );
      })}
      <line x1={0} y1={height} x2={width} y2={height} stroke="currentColor" stroke-opacity={0.2} />
    </svg>
  );
}
