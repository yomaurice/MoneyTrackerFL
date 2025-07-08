'use client';

import { useSearchParams, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

const API_BASE_URL = 'http://localhost:5000/api';

export default function AddTransaction({ onTransactionAdded, transactionToEdit }) {
  const router = useRouter();
  const searchParams = useSearchParams();
//   const id = searchParams.get('id');
// const id = transactionToEdit?.id ?? null;
const idFromQuery = searchParams.get('id');
const id = transactionToEdit?.id ?? idFromQuery;

  const [formData, setFormData] = useState({
    type: 'expense',
    category: '',
    amount: '',
    description: '',
    date: new Date().toISOString().split('T')[0],
  });

  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(!!id);
  const [message, setMessage] = useState('');
  const [isRecurring, setIsRecurring] = useState(false);
  const [recurrenceMonths, setRecurrenceMonths] = useState(1);

  useEffect(() => {
    fetch(`${API_BASE_URL}/categories/${formData.type}`)
      .then((res) => res.json())
      .then((data) => {
        setCategories(data);
        if (!formData.category && data.length > 0) {
          setFormData((prev) => ({ ...prev, category: data[0] }));
        }
      })
      .catch((err) => console.error('Failed to fetch categories:', err));
  }, [formData.type]);

  useEffect(() => {
    if (id) {
      fetch(`${API_BASE_URL}/transactions/${id}`)
        .then((res) => res.json())
        .then((data) => {
          setFormData({
            type: data.type,
            category: data.category,
            amount: data.amount.toString(),
            description: data.description,
            date: data.date,
          });
          setLoading(false);
        })
        .catch((err) => {
          console.error('Failed to load transaction:', err);
          setMessage('Failed to load transaction');
          setLoading(false);
        });
    }
  }, [id]);

useEffect(() => {
  if (transactionToEdit) {
    setFormData({
      type: transactionToEdit.type,
      category: transactionToEdit.category,
      amount: transactionToEdit.amount.toString(),
      description: transactionToEdit.description,
      date: transactionToEdit.date,
    });
  }
}, [transactionToEdit]);
  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);

    const url = id ? `${API_BASE_URL}/transactions/${id}` : `${API_BASE_URL}/transactions`;
    const method = id ? 'PUT' : 'POST';

    const payload = {
      ...formData,
      amount: parseFloat(formData.amount),
    };

    if (isRecurring && recurrenceMonths > 1) {
      payload.is_recurring = true;
      payload.recurrence_months = recurrenceMonths;
    }

    try {
      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (response.ok) {
        setMessage(id ? 'Transaction updated!' : 'Transaction added!');
        onTransactionAdded?.();
        setTimeout(() => {
          setMessage('');
//           router.push('/analytics');
        }, 1000);
      } else {
        const data = await response.json();
        setMessage(data.error || 'Failed to save transaction');
      }
    } catch (err) {
      console.error(err);
      setMessage('Network error');
    } finally {
      setLoading(false);
    }
  };

  if (loading) return <div>Loading...</div>;

  return (
    <div className="bg-white rounded-lg shadow-md p-6">
      <h2 className="text-2xl font-semibold mb-6 text-gray-800">
        {id ? 'Edit Transaction' : 'Add New Transaction'}
      </h2>

      <form onSubmit={handleSubmit} className="space-y-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Transaction Type *
          </label>
          <div className="flex space-x-4">
            <label className="flex items-center">
              <input
                type="radio"
                name="type"
                value="income"
                checked={formData.type === 'income'}
                onChange={handleInputChange}
                className="mr-2"
              />
              <span className="text-green-600 font-medium">Income</span>
            </label>
            <label className="flex items-center">
              <input
                type="radio"
                name="type"
                value="expense"
                checked={formData.type === 'expense'}
                onChange={handleInputChange}
                className="mr-2"
              />
              <span className="text-red-600 font-medium">Expense</span>
            </label>
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Category *
          </label>
          <select
            name="category"
            value={formData.category}
            onChange={handleInputChange}
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            required
          >
            <option value="">Select a category</option>
            {categories.map((category) => (
              <option key={category} value={category}>{category}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Amount ($) *
          </label>
          <input
            type="number"
            name="amount"
            value={formData.amount}
            onChange={handleInputChange}
            step="0.01"
            min="0.01"
            placeholder="0.00"
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            required
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Description
          </label>
          <textarea
            name="description"
            value={formData.description}
            onChange={handleInputChange}
            rows="3"
            placeholder="Optional description..."
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Date
          </label>
          <input
            type="date"
            name="date"
            value={formData.date}
            onChange={handleInputChange}
            className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="flex items-center text-sm font-medium text-gray-700 mb-2">
            <input
              type="checkbox"
              checked={isRecurring}
              onChange={(e) => setIsRecurring(e.target.checked)}
              className="mr-2"
            />
            Recurring Monthly
          </label>
          {isRecurring && (
            <div className="mt-2">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Number of Months
              </label>
              <input
                type="number"
                min="1"
                value={recurrenceMonths}
                onChange={(e) => setRecurrenceMonths(parseInt(e.target.value))}
                className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          )}
        </div>

        <button
          type="submit"
          disabled={loading}
          className={`w-full py-3 px-4 rounded-md font-medium text-white transition-colors ${
            loading ? 'bg-gray-400 cursor-not-allowed' : 'bg-blue-500 hover:bg-blue-600'
          }`}
        >
          {loading ? 'Saving...' : id ? 'Update Transaction' : 'Add Transaction'}
        </button>

        {message && (
          <div
            className={`p-3 rounded-md text-center ${
              message.includes('success')
                ? 'bg-green-100 text-green-800'
                : 'bg-red-100 text-red-800'
            }`}
          >
            {message}
          </div>
        )}
      </form>
    </div>
  );
}
