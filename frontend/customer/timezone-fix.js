function xvondNormalizeUtcTimestamp(value) {
    const raw = String(value || "").trim();
    if (!raw) return raw;
    return /([zZ]|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}Z`;
}

const xvondOriginalFormatDate = typeof formatDate === "function" ? formatDate : null;

formatDate = function(value) {
    if (!value) return "-";
    try {
        const date = new Date(xvondNormalizeUtcTimestamp(value));
        if (Number.isNaN(date.getTime())) {
            return xvondOriginalFormatDate ? xvondOriginalFormatDate(value) : safe(value);
        }
        return safe(date.toLocaleString());
    } catch (_error) {
        return xvondOriginalFormatDate ? xvondOriginalFormatDate(value) : safe(value);
    }
};
