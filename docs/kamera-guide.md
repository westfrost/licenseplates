# Kamera og opsætning: hvad virker, og hvad er billigst?

Opløsningen betyder mindre end man tror. Det vigtigste er, at **pladen fylder nok
i billedet** og at billedet er **skarpt, også i mørke**. Tre ting afgør det:

| Krav | Hvorfor | Hvad du skal kigge efter |
|---|---|---|
| **Smal synsvinkel / zoom** | Pladen skal være mindst ca. 100–150 pixels bred. Med en vidvinkel (2,8 mm) sker det kun, når bilen er 2–3 m fra kameraet. | Varifokal-objektiv (fx 2,7–13,5 mm, motoriseret zoom), som du kan zoome ind på det sted, hvor bilen kører langsomt eller holder |
| **Manuel lukkertid** | Om natten skruer kameraet ned for lukkeren (1/12–1/30 s), og pladen bliver en hvid sløring | Mulighed for at låse lukkertiden til 1/250–1/1000 s. Dahua og Hikvision kan det. De fleste forbrugerkameraer kan ikke |
| **Styrbar IR** | Nummerplader er refleksfolie. Fuld IR om natten "brænder" pladen hvid | Justerbar IR-styrke ("Smart IR") eller en separat IR-projektør, du kan skrue ned for |

Derudover:

- **RTSP / ONVIF**, så programmet kan hente videoen lokalt. Undgå kameraer der kun virker via en cloud-app.
- **PoE (strøm over netkablet)**: ét kabel, stabilt, ingen Wi-Fi-udfald.
- **Ingen batterikameraer** (Ring, Arlo, Eufy-batteri o.l.). De optager kun i klip og har ingen kontinuerlig stream.

## Anbefalinger

Priserne er cirkapriser for 2025/26 og svinger meget. Tjek dagspris (fx AliExpress-butikker for Dahua-OEM, eller danske forhandlere).

### Bedste valg for pengene: Dahua WizSense 4 MP med motorzoom, ca. 1.000–1.600 kr.

- **Dahua IPC-HFW2441T-ZS** (bullet) eller **IPC-HDW2441T-ZS** (turret), eller tilsvarende OEM-modeller (Empiretech, Amcrest m.fl.).
- 4 MP, 2,7–13,5 mm motorzoom, god nattefølsomhed (Starlight), justerbar IR, fuld kontrol over lukkertid, RTSP og PoE.
- Det er den type kamera, hobbyfolk typisk bruger til nummerpladegenkendelse (fx med Frigate), fordi det kan indstilles til netop den opgave.
- Tilsvarende fra Hikvision: **DS-2CD2646G2-IZS** (AcuSense, 2,8–12 mm motorzoom), ofte lidt dyrere.

### Billigst der stadig virker: Reolink med optisk zoom, ca. 800–1.200 kr.

- **Reolink RLC-811A** (4K, 5x optisk zoom, PoE, RTSP).
- Let at sætte op, men har begrænset kontrol over lukkertid og IR. Fint om dagen. Om natten virker det, hvis bilen kører langsomt og der er lidt lys (fx en lampe med bevægelsessensor ved indkørslen).
- Har du kort afstand (bilen holder 3–5 m fra kameraet), kan en **Reolink RLC-510A/520A** med fast objektiv (ca. 400–600 kr.) være nok.

### Overkill / professionelt: dedikerede ANPR-kameraer, ca. 3.000–6.000 kr.

- Fx Hikvision DeepinView ANPR-modeller eller Dahua ITC-serien. De har bedre nattebilleder, men er unødvendige til en privat indkørsel, hvor bilerne kører langsomt.

### Resten af udstyret

| Del | Forslag | Ca. pris |
|---|---|---|
| PoE | PoE-injektor (1 kamera) eller lille PoE-switch, fx TP-Link TL-SG1005P | 150–350 kr. |
| Kabel | Udendørs CAT6-kabel + RJ45-stik | 100–300 kr. |
| Server | Det du har. En hvilken som helst x86-maskine med 2–4 kerner. En Intel N100-mini-PC (ca. 1.200–1.800 kr.) klarer det med god margin. Raspberry Pi 5 virker også, bare med lavere billedrate | 0–1.800 kr. |

**Samlet budget:** ca. 1.300–2.000 kr. for kamera, PoE og kabel, hvis du allerede har en server.

Til reference: på en almindelig 4-kernet server bruger detektor og OCR ca. 30 ms pr. billede. Programmet analyserer kun billeder, når der er bevægelse (5 billeder i sekundet), så CPU-forbruget er lavt i hverdagen.

## Montering

```
                      vej
   ───────────────────────────────────────────
              │                    │
              │   indkørsel        │
              │                    │
              │      ┌──┐          │    ← bilen sætter farten ned / holder her
              │      │  │          │
              │      └──┘          │
              │         ▲          │
              │         │ 3-10 m   │
              └─────────┼──────────┘
                     [kamera]  højde 1-2,5 m, på husmur/carport
```

- **Højde 1–2,5 m.** Hænger kameraet højere, ser det skråt ned på pladen, og bogstaverne bliver svære at læse. Den lodrette vinkel til pladen bør være under ca. 30°.
- **Vinkel fra siden under ca. 30°.** Kig så lige på pladen som muligt.
- **Peg på det sted, hvor bilerne kører langsomt eller holder stille,** ikke ud på vejen.
- **Undgå at forlygterne lyser direkte ind i kameraet.** Er kameraet forskudt lidt til siden og retter sig mod bagpladen, når bilen holder, er det en fordel.
- **Brug zoomen**, så indkørslen fylder billedet. Sæt derefter `roi` i `config.yaml`, så vejen ikke kommer med.

## Indstillinger i kameraet (Dahua/Hikvision)

1. **Lukkertid:** Manuel eller "Shutter priority", 1/250 s (dag) og maks. 1/500 s (nat). Hvis bilerne kører hurtigt forbi: 1/1000 s.
2. **Gain:** Maks. 50–70 om natten. Højere gain giver støj.
3. **IR:** Smart IR / manuel, lavere styrke. Se på live-billedet om natten, om pladen kan læses.
4. **WDR:** Til, hvis indkørslen ligger delvist i skygge.
5. **Stream:** H.264 (ikke H.265+), 10–15 fps, main stream 2560×1440 eller sub-stream 1280×720, hvis pladen er stor nok.
6. **Fokus:** Brug autofokus efter zoom, og tjek skarpheden om natten. IR kan flytte fokus.

## Lovgivning (Danmark)

Dette er ikke juridisk rådgivning, men vær opmærksom på følgende:

- **Tv-overvågningsloven:** Som privatperson må du overvåge din egen grund, men ikke offentlig vej, fortov eller naboens grund. Ret kameraet ind på indkørslen, og brug `roi` til at udelukke vejen.
- **GDPR:** Nummerplader er personoplysninger. Ifølge EU-Domstolen (sag C-212/13, Ryneš) gælder husholdningsundtagelsen ikke, hvis kameraet også dækker offentligt område, og så gælder GDPR fuldt ud. Endnu en grund til at holde kameraet inden for egen grund.
- Gem ikke data længere end nødvendigt. Billeder slettes automatisk efter `snapshot_retention_days` (standard 90 dage).
- Se Datatilsynets vejledning om tv-overvågning for de nyeste regler.

Hændelsestypen "forbi" findes til kameraer, der ser en fælles privat vej eller lignende. Peger kameraet ud på offentlig vej, bør du snævre `roi` ind i stedet for at registrere forbikørende.
