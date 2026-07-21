# Tworzenie przewodników PDF krok po kroku

Guidebot potrafi wyrenderować skompilowany scenariusz jako krajobrazowy przewodnik PDF — jeden
anotowany krok na stronę, obok tekstu narracji. Każda strona przewodnika zamraża ten kadr, który
najlepiej tłumaczy dany krok — dla większości akcji moment jej zakończenia, dla `select` moment,
w którym lista opcji jest rozwinięta — i nakłada na niego adnotacje: strzałkę ruchu kursora, ramkę
wokół celu akcji, gwiazdkę w miejscu kliknięcia i elipsę zakreślenia.

Ta funkcja nie wymaga LLM ani dodatkowych zależności poza skompilowanym sidecarem.

## Przegląd

Wygeneruj przewodnik PDF z już skompilowanego scenariusza:

```bash
uv run guidebot guide scenarios/login.scenario.yaml --out out/login-guide.pdf
```

Polecenie:

- Wczytuje źródłowy `login.scenario.yaml` i jego sidecar `login.compiled.yaml`;
- Otwiera świeży Chromium w skonfigurowanym viewporcie i locale;
- Przechodzi przez każdą akcję, przechwytując zrzuty ekranu i budując anotowane klatki;
- Eksportuje krajobrazowy PDF: zrzut ekranu po lewej, narracja po prawej.

Przewodnik wymaga wcześniejszego pomyślnego kroku `compile`. Nie produkuje wywołań LLM, syntezy
TTS ani wideo.

## Układ i typy stron

Jeden przewodnik PDF zawiera jedną lub więcej stron:

- **Interaktywny krok (click, hover, type)** — Anotowany zrzut ekranu (lewo), tekst narracji (prawo).
- **Krok z listą rozwijaną (`select`)** — Ten sam układ, ale kadr jest robiony **przy rozwiniętej
  liście opcji**. Patrz [Listy rozwijane](#listy-rozwijane-select).
- **Nawigacja** — Strona z tekstem „Otwórz adres:" i adresem URL (kroki `navigate`).
- **Plansza podziału** — Karta w stylu slajdu wstawiona jako przerwa wizualna (kroki `slide`).
- **Bramy wait/when** — Bez strony. Warunkowe czekania i tło nie produkują wyjścia.

### Legenda adnotacji

Zrzuty ekranu są nakładane adnotacjami:

- **Strzałka** (prosty odcinek) — Ruch kursora z poprzedniego celu do obecnego. Biegnie
  między ramkami, a nie przez ich środki. Gdy cele nachodzą na siebie albo dzieli je mniej
  niż 12 px, strzałki nie ma wcale.
- **Czerwona ramka** — Cel akcji: kliknięcia, wpisania tekstu, najechania lub wyboru
  z listy.
- **Gwiazdka** — Miejsce kliknięcia myszą: ośmioramienna gwiazdka wokół kursora,
  z przerwą w środku, żeby sam kursor pozostał widoczny.
- **Elipsa** — Zakreślenie z kroku `highlight`, w kolorze ustawionym w scenariuszu.
  Zamiast okrężnego ruchu kursora, który widać w filmie, przewodnik pokazuje samą
  gotową elipsę wokół wskazanego elementu lub obszaru.

## Listy rozwijane (`select`)

Strona kroku `select` jest fotografowana **w trakcie interakcji**: lista opcji jest rozwinięta,
a wybierana opcja jest oznaczona gwiazdką tak samo jak cel kroku `click`. To jedyna akcja, której
znaki rozkładają się na dwa prostokąty:

- **gwiazdka** na wierszu opcji — to, co czytelnik ma kliknąć, rysowana dokładnie jak na stronie
  kroku `click`;
- **czerwona ramka** wokół samej kontrolki, żeby było widać, w którym polu jesteśmy;
- **strzałka** kończąca się na krawędzi wiersza opcji, a nie kontrolki.

Strzałka kolejnego kroku zaczyna się od tego wiersza — tam, gdzie zostało oko czytelnika.

W trybie `mode: native` (poniżej) nie ma wiersza, więc strona kroku `select` jest oznaczona jak
każda inna akcja z ramką: strzałka do ramki kontrolki i sama ramka, bez gwiazdki — nic widocznego
nie jest klikane.

Działa to dlatego, że Guidebot wstrzykuje nakładkę DOM zastępującą natywną listę opcji (tę samą,
która pokazuje listy rozwijane na filmach z `render`) — listę natywnego `<select>` rysuje system
operacyjny i żadne narzędzie do automatyzacji przeglądarki nie potrafi jej zrzucić. Strony, które
ulepszają swoje selecty same (select2, Tom Select, Chosen), mają już listę w DOM i przewodnik
steruje właśnie nią.

`config.selects.mode: native`, albo `mode: native` na pojedynczym kroku, wyłącza nakładkę: kursor
nadal dojeżdża do kontrolki, wartość nadal zostaje wybrana, ale kadr pokazuje zwiniętą kontrolkę,
a strona niesie tylko czerwoną ramkę — nie ma czego rozwijać. Użyj tego dla widżetu, którego
przewodnik nie potrafi obsłużyć; komunikat błędu mówi wprost, o którą sytuację chodzi, i podaje
nazwę szukanej opcji.

Obie opcje są opisane w [referencji scenariusza](scenario-reference.md).

Krok z `optional: true` jest pomijany, gdy lista **nie zawiera** szukanej opcji — to
jedyna sytuacja, w której ciche pominięcie jest poprawne, bo dokładnie o tym mówi
`optional`. Każda inna porażka listy rozwijanej (kliknięcie, które nie zmieniło wyboru;
widżet, którego nie da się rozwinąć; nakładka zdjęta w trakcie kroku) zatrzymuje
przewodnik także dla kroku opcjonalnego — inaczej strona zniknęłaby po cichu z PDF-a,
a usterka została na stronie.

Dwa przypadki warto wymienić wprost, bo bywają brane za „brak opcji", a nim nie są.
Opcja `disabled` **jest** na liście — strona po prostu jej nie przyjmuje — więc krok
`optional: true` zatrzyma się na niej, zamiast ją pominąć; inaczej przewodnik po cichu
przestałby pokazywać kontrolkę, którą strona celowo zablokowała. Tak samo lista
`multiple` / `size > 1`, która nie ma na stronie żadnego rozmiaru: o jej opcjach nie
dowiedzieliśmy się wtedy niczego, a szukana może tam być.

## Tekst narracji: `say`, `teach` lub `caption`

Domyślnie strona PDF pokazuje narrację kroku — albo `say` dla samodzielnych kroków, albo `teach`
dla nauczonych akcji. Tekst jest wyświetlony bez syntezy TTS.

Aby nadpisać tekst PDF dla jednego kroku, użyj opcjonalnego pola `caption:`:

```yaml
steps:
  - teach: "Kliknij niebieski przycisk logowania, aby przejść dalej"
    caption: "Zaloguj się"
```

W tym przykładzie wyrenderowany PDF pokazuje „Zaloguj się" zamiast pełnego tekstu `teach`. Pole
`caption` jest ignorowane przez `render` i nie wpływa na skomponowane wideo. Gdy go pominiesz,
Guidebot wraca do `say` lub `teach` jak zwykle.

## Opcje wiersza poleceń

```bash
uv run guidebot guide SCENARIUSZ.yaml --out WYNIK.pdf [OPCJE]
```

| Opcja | Domyślnie | Znaczenie |
|---|---:|---|
| `--out ŚCIEŻKA`, `-o ŚCIEŻKA` | wymagana | Docelowy `.pdf`. Katalogi-rodzice są tworzone. |
| `--headed` | wyłączone | Pokazuje Chromium. |
| `--pause-on-error` | wyłączone | Po błędzie zatrzymuje widoczną stronę do inspekcji. |
| `--timeout SEKUNDY` | `15` | Timeout akcji Playwrighta. |
| `--verbose`, `-v` | wyłączone | Pasek postępu (jak w `render`), rodzaj każdego kroku i komunikaty o pomijaniu. |

Wartość `--timeout` jest używana identycznie do poleceń `compile` i `render` i dotyczy
wszystkich akcji przeglądarki podczas generowania przewodnika.

`--headed` i `--pause-on-error` działają tak samo jak w `compile` i `render`: to
narzędzia diagnostyczne, gdy przechwytywanie zachowuje się inaczej niż oczekujesz.
Domyślnie przewodnik powstaje bez widocznego okna, ale nie jest to wymóg —
składanie PDF działa w obu trybach.

## Obecne ograniczenia v1

Funkcja przewodnika ma następujący zakres:

- **Tylko jeden język** — Przewodniki używają kanonicznej narracji z `say` i `teach`; wielojęzyczne
  ścieżki audio i `translations` nie są obsługiwane. Aby stworzyć zlokalizowane przewodniki,
  skompiluj i zbuduj osobne pliki PDF dla każdego locale.
- **Brak scenariuszy z popupami** — Scenariusz otwierający popup jest odrzucany z wyraźnym błędem.
  Funkcja wykrywa to podczas fazy preflight i kończy się bez tworzenia wyjścia.
- **Brak grupowania wieloetapowego** — Kroki są renderowane indywidualnie. Przyszłe wersje mogą
  pozwolić na wizualne grupowanie sekwencji kroków lub numerowanie (np. „Krok 1 z 5").
- **Brak dostosowania layoutu PDF** — Marginesy, czcionki, kolory i wymiary strony są ustalone.
- **`scroll` własną stronę produkuje tylko z tekstem** — Krok `scroll` zawsze faktycznie przewija
  stronę (zrzuty są robione z widocznego obszaru viewportu, więc przewinięcie jest konieczne, by
  kolejne kroki pokazywały właściwy fragment), ale własną stronę PDF tworzy tylko wtedy, gdy niesie
  `say` lub `caption`. Sam `scroll` bez tekstu tylko przygotowuje widok pod kolejny krok.

Jeśli Twój scenariusz wykracza poza te ograniczenia, użyj `render` do produkcji MP4.

## Walidacja skompilowanego sidecara

Przed uruchomieniem `guide` upewnij się, że scenariusz się kompiluje pomyślnie:

```bash
uv run guidebot validate scenarios/login.scenario.yaml
uv run guidebot compile scenarios/login.scenario.yaml --headed -v
uv run guidebot guide scenarios/login.scenario.yaml --out out/guide.pdf --verbose
```

Jeśli scenariusz otwiera popup, polecenie `guide` będzie się nie powieść przy uruchomieniu
z wyraźnym błędem i kodem wyjścia 2. Warunkowe gałęzie `when:` są obsługiwane automatycznie —
brak elementu warunkującego oznacza, że cała gałąź jest pomijana i nie tworzy stron. Użyj
`compile --force` jeśli edytowałeś scenariusz, potem spróbuj ponownie `guide`.
