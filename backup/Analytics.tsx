
import { useState, useEffect, useMemo } from 'react';
import { useRouter } from 'next/navigation'; // Add this at the top if not already

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ResponsiveContainer,
} from 'recharts';

const API_BASE_URL = 'http://localhost:5000/api';

const formatCurrency = (amount) => {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  }).format(amount || 0);
};

const getCurrentYearMonth = () => {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${d.getFullYear()}-${mm}`;
};

export default function Analytics({ onEdit }) {
//   const router = useRouter();
//   const [isClient, setIsClient] = useState(false);
//
//     // Add this useEffect
//     useEffect(() => {
//       setIsClient(true);
//     }, []);


  const [viewMode, setViewMode] = useState('monthly');
  const [selectedMonth, setSelectedMonth] = useState(getCurrentYearMonth());
  const [rawAnalytics, setRawAnalytics] = useState(null);
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [loading, setLoading] = useState(false);



  useEffect(() => {
    fetchAnalytics();
  }, [categoryFilter]);

  const fetchAnalytics = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        period: 'monthly',
        categories: categoryFilter,
      });
      const res = await fetch(`${API_BASE_URL}/analytics?${params}`);
      if (!res.ok) {
        console.error('Failed to load analytics:', res.statusText);
        setRawAnalytics(null);
      } else {
        const data = await res.json();
        setRawAnalytics(data);
      }
    } catch (e) {
      console.error(e);
      setRawAnalytics(null);
    } finally {
      setLoading(false);
    }
  };

  const currentYear = new Date().getFullYear().toString();

  const computeYearlySummary = () => {
    if (!rawAnalytics?.summary) return { income: 0, expense: 0 };
    let totalInc = 0;
    let totalExp = 0;
    Object.entries(rawAnalytics.summary).forEach(([period, vals]) => {
      if (period.startsWith(currentYear + '-')) {
        totalInc += vals.income || 0;
        totalExp += vals.expense || 0;
      }
    });
    return { income: totalInc, expense: totalExp };
  };

  const buildYearlyChartData = () => {
    if (!rawAnalytics?.summary) return [];
    const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const arr = [];
    for (let m = 1; m <= 12; m++) {
      const mm = String(m).padStart(2, '0');
      const key = `${currentYear}-${mm}`;
      let expense = 0;
      if (categoryFilter === 'all') {
        expense = rawAnalytics.summary[key]?.expense || 0;
      } else {
        const breakdown = rawAnalytics.categoryBreakdown[key]?.expense || {};
        expense = breakdown[categoryFilter] || 0;
      }
      arr.push({ monthLabel: monthNames[m - 1], expense });
    }
    return arr;
  };

  const buildYearlyExpenseList = () => {
    if (!rawAnalytics?.details) return [];
    let allTx = [];
    Object.entries(rawAnalytics.details).forEach(([periodKey, txArray]) => {
      if (periodKey.startsWith(currentYear + '-')) {
        allTx = allTx.concat(
          txArray.filter(
            (tx) => tx.type === 'expense' && (categoryFilter === 'all' || tx.category === categoryFilter)
          )
        );
      }
    });
    return allTx.sort((a, b) => (a.date < b.date ? 1 : -1));
  };

  const computeMonthlySummary = () => {
    if (!rawAnalytics?.summary) return { income: 0, expense: 0 };
    const vals = rawAnalytics.summary[selectedMonth] || { income: 0, expense: 0 };
    return { income: vals.income || 0, expense: vals.expense || 0 };
  };

  const buildMonthlyChartData = () => {
    if (!rawAnalytics?.categoryBreakdown) return [];
    const expObj = rawAnalytics.categoryBreakdown[selectedMonth]?.expense || {};
    if (categoryFilter === 'all') {
      return Object.entries(expObj).map(([cat, amt]) => ({ category: cat, expense: amt }));
    } else {
      return [{ category: categoryFilter, expense: expObj[categoryFilter] || 0 }];
    }
  };

  const buildMonthlyExpenseList = () => {
    if (!rawAnalytics?.details) return [];
    const arr = rawAnalytics.details[selectedMonth] || [];
    return arr
      .filter(
        (tx) => tx.type === 'expense' && (categoryFilter === 'all' || tx.category === categoryFilter)
      )
      .sort((a, b) => (a.date < b.date ? 1 : -1));
  };

  const expenseCategories = useMemo(() => {
    if (!rawAnalytics?.categoryBreakdown) return [];
    const allCategories = new Set();
    Object.values(rawAnalytics.categoryBreakdown).forEach((entry) => {
      const expenses = entry.expense || {};
      Object.keys(expenses).forEach((cat) => allCategories.add(cat));
    });
    return Array.from(allCategories).sort();
  }, [rawAnalytics]);

  if (loading) return <div className="bg-white p-6 text-center">Loading…</div>;
  if (!rawAnalytics) return <div className="bg-white p-6 text-center">No data available.</div>;

  const isYearly = viewMode === 'yearly';
  const { income, expense } = isYearly ? computeYearlySummary() : computeMonthlySummary();
  const net = income - expense;
  const chartData = isYearly ? buildYearlyChartData() : buildMonthlyChartData();
  const expenseList = isYearly ? buildYearlyExpenseList() : buildMonthlyExpenseList();


    const handleEdit = (tx) => {
       onEdit(tx);
    };
//    const handleEdit = (tx) => {
//   const query = new URLSearchParams({
//     id: tx.id,
//     type: tx.type,
//     category: tx.category,
//     amount: tx.amount,
//     description: tx.description,
//     date: tx.date,
//   }).toString();
//
//   router.push(`/addTransaction?${query}`);
// };

    const handleDelete = async (id) => {
      if (!window.confirm('Are you sure you want to delete this transaction?')) return;

      try {
        const res = await fetch(`${API_BASE_URL}/transactions/${id}`, {
          method: 'DELETE',
        });

        if (!res.ok) throw new Error('Failed to delete');

        // Refetch data
        fetchAnalytics();
      } catch (err) {
        console.error('Delete error:', err);
        alert('Failed to delete transaction.');
      }
    };


  return (
    <div className="space-y-6 px-4 md:px-6">
      {/* ─── HEADER & FILTERS ─────────────────────────────────────────── */}
      <div className="bg-white rounded-lg shadow-md p-6">
        <h2 className="text-2xl font-semibold mb-4 text-gray-800">Financial Analytics</h2>

        <div className="flex flex-wrap gap-6 items-end">
          {/* View Mode */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              View
            </label>
            <select
              value={viewMode}
              onChange={(e) => setViewMode(e.target.value)}
              className="px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="monthly">Monthly</option>
              <option value="yearly">Yearly</option>
            </select>
          </div>

          {/* If Monthly, show Month picker */}
          {!isYearly && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Month
              </label>
              <input
                type="month"
                className="px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                value={selectedMonth}
                onChange={(e) => setSelectedMonth(e.target.value)}
              />
            </div>
          )}

          {/* Category Filter */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Categories
            </label>
          <select
  value={categoryFilter}
  onChange={(e) => setCategoryFilter(e.target.value)}
  className="border p-1 rounded"
>
  <option value="all">All Categories</option>
  {expenseCategories.map((cat) => (
    <option key={cat} value={cat}>
      {cat}
    </option>
  ))}
</select>



          </div>
        </div>
      </div>

      {/* ─── MAIN GRID: Chart + Summary + List ─────────────────────────────┐ */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/** ── LEFT COLUMN (List of Expenses) ─────────────────────────── */}
        <div className="lg:col-span-1 order-2 lg:order-1">
          <div className="bg-white rounded-lg shadow-md p-4 h-full flex flex-col">
            <h3 className="text-lg font-semibold mb-3 text-gray-800">
              {isYearly
                ? `Latest Expenses (${currentYear})`
                : `Expenses in ${new Date(
                    ...selectedMonth.split('-').map((s, i) =>
                      i === 1 ? parseInt(s) - 1 : parseInt(s)
                    )
                  ).toLocaleDateString('en-US', {
                    year: 'numeric',
                    month: 'long',
                  })}`}
            </h3>
            <div className="overflow-y-auto flex-1">
              {expenseList.length === 0 && (
                <div className="text-sm text-gray-500">No expenses to show.</div>
              )}
              {expenseList.map((tx) => (
  <div key={tx.id} className="border-b border-gray-200 py-2 last:border-none">
    <div className="flex justify-between items-center text-sm">
      <span className="font-medium text-gray-700">{tx.category}</span>
      <span className="text-red-600">{formatCurrency(tx.amount)}</span>
    </div>

    <div className="flex justify-between items-center text-xs text-gray-500 mt-0.5">
      <span>
        {new Date(tx.date).toLocaleDateString('en-US', {
          year: 'numeric',
          month: 'short',
          day: 'numeric',
        })}
      </span>
      <span className="italic">{tx.description}</span>
    </div>

    {/* 👇 Buttons Row */}
    <div className="mt-2 flex gap-2 text-xs">
      <button
        onClick={() => handleEdit(tx)}
        className="px-2 py-1 bg-blue-100 text-blue-700 rounded hover:bg-blue-200"
      >
        Edit
      </button>
      <button
        onClick={() => handleDelete(tx.id)}
        className="px-2 py-1 bg-red-100 text-red-700 rounded hover:bg-red-200"
      >
        Delete
      </button>
    </div>
  </div>
))}

            </div>
          </div>
        </div>

        {/** ── MIDDLE (Bar Chart) and RIGHT (Summary Box) ─────────────── */}
        <div className="lg:col-span-2 flex flex-col gap-6 order-1 lg:order-2">
          {/** ── SUMMARY BOX at top-right ─────────────────────────── */}
          <div className="flex justify-end">
            <div className="bg-white rounded-lg shadow-md p-4 w-full md:w-1/2 lg:w-4/5">
              <h3 className="text-lg font-medium mb-3 text-gray-800">
                {isYearly ? `Year ${currentYear} Summary` : 'This Month Summary'}
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                {/* Income */}
                <div className="bg-green-50 p-3 rounded-lg">
                  <div className="text-sm text-green-600 font-medium">Income</div>
                  <div className="text-2xl font-bold text-green-700">
                    {formatCurrency(income)}
                  </div>
                </div>
                {/* Expenses */}
                <div className="bg-red-50 p-3 rounded-lg">
                  <div className="text-sm text-red-600 font-medium">Expenses</div>
                  <div className="text-2xl font-bold text-red-700">
                    {formatCurrency(expense)}
                  </div>
                </div>
                {/* Net */}
                <div
                  className={`p-3 rounded-lg ${
                    net >= 0 ? 'bg-blue-50' : 'bg-orange-50'
                  }`}
                >
                  <div
                    className={`text-sm font-medium ${
                      net >= 0 ? 'text-blue-600' : 'text-orange-600'
                    }`}
                  >
                    Net
                  </div>
                  <div
                    className={`text-2xl font-bold ${
                      net >= 0 ? 'text-blue-700' : 'text-orange-700'
                    }`}
                  >
                    {formatCurrency(net)}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/** ── BAR CHART ───────────────────────────────────────────── */}
          <div className="bg-white rounded-lg shadow-md p-4 flex-1">
            <h3 className="text-lg font-semibold mb-4 text-gray-800">
              {isYearly
                ? 'Expenses Per Month'
                : `Expenses by Category (${new Date(
                    ...selectedMonth.split('-').map((s, i) =>
                      i === 1 ? parseInt(s) - 1 : parseInt(s)
                    )
                  ).toLocaleDateString('en-US', {
                    year: 'numeric',
                    month: 'long',
                  })})`}
            </h3>

            {chartData.length === 0 ? (
              <div className="text-center text-gray-500">
                No data to draw chart.
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  {isYearly ? (
                    <>
                      <XAxis dataKey="monthLabel" />
                      <YAxis />
                      <Tooltip
                        formatter={(value) => formatCurrency(value)}
                      />
                      <Bar
                        dataKey="expense"
                        name="Expense"
                        fill="#f87171" /* red-400 */
                      />
                    </>
                  ) : (
                    <>
                      <XAxis dataKey="category" tick={{ angle: -30, textAnchor: 'end' }} height={70} />
                      <YAxis />
                      <Tooltip
                        formatter={(value) => formatCurrency(value)}
                      />
                      <Bar
                        dataKey="expense"
                        name="Expense"
                        fill="#f87171"
                      />
                    </>
                  )}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
