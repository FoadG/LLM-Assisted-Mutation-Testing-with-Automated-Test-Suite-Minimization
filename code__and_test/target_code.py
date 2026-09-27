"""
target_code.py
--------------
کد هدف نمونه برای آزمایش سیستم mutation testing.

شامل:
  - bubble_sort: مرتب‌سازی حبابی (in-place)
  - binary_search: جستجوی دودویی

این فایل توسط کاربر ارائه می‌شود.
سیستم روی این فایل Mutant تولید می‌کند.
"""


def bubble_sort(arr: list) -> list:
    """
    مرتب‌سازی حبابی.
    ورودی را in-place مرتب می‌کند و لیست مرتب‌شده را برمی‌گرداند.
    """
    n = len(arr)
    for i in range(n):
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return arr


def binary_search(arr: list, target: int) -> int:
    """
    جستجوی دودویی در لیست مرتب‌شده.
    Returns:
        اندیس target اگر پیدا شد، در غیر این صورت -1
    """
    left = 0
    right = len(arr) - 1

    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1

    return -1
