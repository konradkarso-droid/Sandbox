package stend.core;

import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Малый JSON: запись и разбор. Своё, а не org.json, чтобы ведущий
 * собирался и проверялся на обычной JVM так же, как на Android.
 *
 * Разбор отдаёт: Map (порядок полей сохранён), List, String, Long или
 * Double, Boolean, null. Байты пишутся объектом {"b64": …} — так их
 * передаёт протокол исполнителя (контракт шага 7, §1).
 */
public final class Json {
    private Json() {
    }

    public static String str(Object o) {
        StringBuilder b = new StringBuilder();
        write(b, o);
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    private static void write(StringBuilder b, Object o) {
        if (o == null) {
            b.append("null");
        } else if (o instanceof String) {
            quote(b, (String) o);
        } else if (o instanceof Boolean || o instanceof Integer || o instanceof Long) {
            b.append(o);
        } else if (o instanceof Double || o instanceof Float) {
            double d = ((Number) o).doubleValue();
            b.append(Double.isNaN(d) || Double.isInfinite(d) ? "null" : Double.toString(d));
        } else if (o instanceof byte[]) {
            b.append("{\"b64\":");
            quote(b, Base64.getEncoder().encodeToString((byte[]) o));
            b.append('}');
        } else if (o instanceof Map) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<Object, Object> e : ((Map<Object, Object>) o).entrySet()) {
                if (!first) b.append(',');
                first = false;
                quote(b, String.valueOf(e.getKey()));
                b.append(':');
                write(b, e.getValue());
            }
            b.append('}');
        } else if (o instanceof List) {
            b.append('[');
            boolean first = true;
            for (Object x : (List<Object>) o) {
                if (!first) b.append(',');
                first = false;
                write(b, x);
            }
            b.append(']');
        } else {
            throw new IllegalArgumentException("не умею писать в JSON: " + o.getClass());
        }
    }

    private static void quote(StringBuilder b, String s) {
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        b.append('"');
    }

    public static Object parse(String s) {
        P p = new P(s);
        Object v = p.value();
        p.ws();
        if (p.i != s.length()) throw new IllegalArgumentException("лишнее после JSON на месте " + p.i);
        return v;
    }

    /** Карта для удобства: new Json.M().put(…) с порядком полей. */
    public static final class M extends LinkedHashMap<String, Object> {
        public M with(String k, Object v) {
            put(k, v);
            return this;
        }
    }

    private static final class P {
        final String s;
        int i;

        P(String s) {
            this.s = s;
        }

        void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        }

        IllegalArgumentException err(String what) {
            return new IllegalArgumentException(what + " на месте " + i);
        }

        Object value() {
            ws();
            if (i >= s.length()) throw err("конец текста");
            char c = s.charAt(i);
            if (c == '{') return object();
            if (c == '[') return array();
            if (c == '"') return string();
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            if (s.startsWith("null", i)) { i += 4; return null; }
            return number();
        }

        Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++;
            ws();
            if (i < s.length() && s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String k = string();
                ws();
                if (i >= s.length() || s.charAt(i) != ':') throw err("нет двоеточия");
                i++;
                m.put(k, value());
                ws();
                if (i >= s.length()) throw err("объект не закрыт");
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw err("нет запятой в объекте");
            }
        }

        List<Object> array() {
            List<Object> a = new ArrayList<>();
            i++;
            ws();
            if (i < s.length() && s.charAt(i) == ']') { i++; return a; }
            while (true) {
                a.add(value());
                ws();
                if (i >= s.length()) throw err("массив не закрыт");
                char c = s.charAt(i++);
                if (c == ']') return a;
                if (c != ',') throw err("нет запятой в массиве");
            }
        }

        String string() {
            if (i >= s.length() || s.charAt(i) != '"') throw err("ждал строку");
            i++;
            StringBuilder b = new StringBuilder();
            while (true) {
                if (i >= s.length()) throw err("строка не закрыта");
                char c = s.charAt(i++);
                if (c == '"') return b.toString();
                if (c != '\\') { b.append(c); continue; }
                char e = s.charAt(i++);
                switch (e) {
                    case '"': b.append('"'); break;
                    case '\\': b.append('\\'); break;
                    case '/': b.append('/'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'n': b.append('\n'); break;
                    case 'r': b.append('\r'); break;
                    case 't': b.append('\t'); break;
                    case 'u':
                        b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default: throw err("неизвестный escape");
                }
            }
        }

        Object number() {
            int st = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String t = s.substring(st, i);
            if (t.isEmpty()) throw err("ждал значение");
            if (t.contains(".") || t.contains("e") || t.contains("E")) return Double.parseDouble(t);
            return Long.parseLong(t);
        }
    }
}
