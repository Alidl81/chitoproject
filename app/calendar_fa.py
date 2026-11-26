from datetime import date, timedelta

# Break years for the Iranian solar calendar, using the Borkowski algorithm.
_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635,
           2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)


def new_year(year: int) -> date:
    if not 1 <= year < _BREAKS[-1]:
        raise ValueError("unsupported solar year")
    gregorian = year + 621
    leap = -14
    previous = _BREAKS[0]
    for following in _BREAKS[1:]:
        jump = following - previous
        if year < following:
            break
        leap += jump // 33 * 8 + (jump % 33) // 4
        previous = following
    offset = year - previous
    leap += offset // 33 * 8 + (offset % 33 + 3) // 4
    if jump % 33 == 4 and jump - offset == 4:
        leap += 1
    gregorian_leap = gregorian // 4 - (gregorian // 100 + 1) * 3 // 4 - 150
    return date(gregorian, 3, 20 + leap - gregorian_leap)


def solar_parts(day: date) -> tuple[int, int, int]:
    year = day.year - 621
    if day < new_year(year):
        year -= 1
    offset = (day - new_year(year)).days
    if offset < 186:
        return year, 1 + offset // 31, 1 + offset % 31
    return year, 7 + (offset - 186) // 30, 1 + (offset - 186) % 30


def from_solar(year: int, month: int, day: int) -> date:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError("invalid solar date")
    offset = (month - 1) * 31 if month <= 6 else 186 + (month - 7) * 30
    result = new_year(year) + timedelta(days=offset + day - 1)
    if solar_parts(result) != (year, month, day):
        raise ValueError("invalid solar date")
    return result


def solar_date(day: date) -> str:
    year, month, date_num = solar_parts(day)
    return f"{year:04d}/{month:02d}/{date_num:02d}"


def parse_solar(value: str) -> date:
    return from_solar(*(int(p) for p in value.split("/")))
