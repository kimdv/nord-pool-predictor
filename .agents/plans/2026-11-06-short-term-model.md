# Plan: særskilt model til de første 48 timer

Dato: 6. november 2026. Status: plan, ikke en implementering eller deploygodkendelse.

## Mål og afgrænsning

Forbedr prognosen for endnu ukendte spotpriser i DK1 og DK2 i de første 48 timer.
Afprøv en korttidsmodel ved siden af den nuværende model. Tag den kun i brug i
områder, hvor en fair sammenligning viser forbedring. Behold den nuværende
modeltype til resten af syvdøgnsvisningen og den eksisterende ottendedagsbuffer.

To modeller betyder to horisontspecialister pr. område, hver med P10/P50/P90.
De køres sekventielt i den eksisterende worker; ingen ekstra container, scheduler,
afhængighed eller generel modelplatform. Dashboardet skal fortsat vise én kurve.

Denne opgave leverer kun planen og review af planen. Træning, kodeændringer,
datamigreringer, commits, pushes og deployment hører til en senere implementering.

## Verificeret udgangspunkt

- `ml/features.py::build_feature_matrix` tildeler hvert træningstidspunkt én
  tilfældig horisont mellem 1 og 768 kvarterer. `horizon_step` og maskering af
  utilgængelige lag bruges allerede i både træning og inference.
- `ml/train.py` optimerer med 50 Optuna-forsøg og tidsordnet validering. De nyeste
  20 % reserveres som holdout; træningslabels ved eller efter tidligste
  valideringsudstedelse fjernes. Registrering deaktiverer i dag alle aktive
  modeller for området, så to uafhængige aktive registreringer virker ikke.
- `ml/artifacts.py`, `ml/predict.py` og `scheduler/bootstrap.py` forventer ét
  P10/P50/P90-sæt og én valgt version pr. område. API, modelvisning og historik
  henviser også til én `model_version` pr. forecast-run.
- `ml/horizon.py` kræver 768 kvarterer: syv synlige dage plus en dags buffer.
  `ml/score.py` har eksisterende 24/72/168-timersmålinger; de er ikke i sig selv
  en test af forbedring på ukendte priser i intervallet 24–48 timer.
- Arbejdstræet indeholder et separat, ikke committet forsøg med priskorrektion:
  `ml/calibration.py`, integration i `ml/predict.py`, benchmark, tests og README.
  Det er ikke en dokumenteret forbedring af den nye model. Bevar disse ændringer
  under planarbejdet; de må ikke utilsigtet komme med i modeludrulningen.

## 1. Frys sammenligningen og datagrundlaget

1. Opret et isoleret implementeringscheckout fra en registreret commit uden
   priskorrektionen. Gem forsøgsrapport, datagrænser, seed, kodeversion,
   modelparametre og inputdækning. Ændr ikke produktionsmodeller eller data.
2. Sammenlign to varianter med samme eksisterende featurekontrakt:
   - Baseline: den nuværende modelstrategi med horisonter 1–768.
   - Kandidat: samme modeltype, men horisonter 1–192 og tuning på korttidsfejl.
   Begge trænes på samme tilladte targethistorik. Kandidatens horisontsampling
   laves på ny inden for 1–192; filtrér ikke blot tre fjerdedele af rækkerne væk.
3. Genbrug as-of-udvælgelsen for vejr, produktion og udlandsforbindelser. Mål og
   rapportér manglende input pr. område, periode og horisont. Opfind ikke
   historiske prognosesnapshots ud fra senere observationer.
4. Offentliggjorte fremtidige spotpriser må i første forsøg kun bruges til at
   identificere, hvilke targets brugeren allerede kender, ikke som nye features.
   Nye prisfeatures, ændret produktionsbackfill, ekstra parameterjagt og en
   tredje, vægtet modelvariant udsættes, så forsøget isolerer horisontopdelingen.

Leverance: reproducerbart eksperiment med baseline og kandidat, uden registrering
som aktiv model og uden at skrive forsøgsruns til tabeller/views for seneste
produktionsprognose. Artefakter og resultater gemmes i en separat forsøgsmappe.

## 2. Test uden fremtidig viden

- Frys én fælles tidsmæssig grænse for development og afsluttende test før tuning.
  Brug samme purgede developmentperiode og samme testudstedelser for begge
  varianter; en kortere horisont må ikke give kandidaten nyere træningslabels.
  Testperioden bruges ikke til Optuna, featurevalg eller gentagen tærskeljustering.
- Genfit også baseline før testperioden. Den aktive model, som allerede er trænet
  på nyere targets, må ikke evalueres bagud på de samme targets som en fair baseline.
- Genskab udstedelser på de eksisterende lokale køretidspunkter 00:05, 06:05,
  12:05 og 18:05, med samme faktiske udstedelsestid og targetgrid i begge varianter.
  Evaluer særskilt intervallet 0–24 timer og 24–48 timer, samt samlet 0–48 timer.
- Gem ved hver udstedelse det præcise sæt af allerede modtagne spotpriser.
  Ekskludér disse targets fra begge modellers fejlmål. Historiske prisrækker har
  kun `created_at` og overskrives ved revision; nuværende værdier og rekonstrueret
  publiceringstid giver derfor ikke et verificeret historisk prisarkiv.
- Brug verificerede, uforanderlige snapshots, hvor de findes. Ellers markeres
  historisk replay som eksplorativt og suppleres med fremadrettet prøve, hvor
  input, tilgængelighed og begge prognoser arkiveres ved udstedelsen. Ingen
  promotion alene på det gamle korrektionseksperiments rekonstruerede data.
- Den afsluttende verificerede test skal dække mindst 28 forskellige lokale
  udstedelsesdatoer pr. område og mindst 1.000 parrede ukendte targetkvarterer i
  hvert af de to døgnintervaller. Manglende data betyder afventning, ikke bestået.
- Rapportér MAE, RMSE, bias, P10/P50/P90-pinball loss, P10–P90-dækning og antal
  parrede targetkvarterer. Vis daglige parrede resultater; behandl ikke overlappende
  kvartaler fra flere runs som uafhængige dage.

Foreslået acceptregel, der fastlåses før eksperimentet køres: mindst 5 % lavere
samlet MAE på ukendte 0–48-timerspriser pr. område; højst 5 % højere MAE i hvert
delinterval; absolut bias må højst stige 0,02 DKK/kWh; hvert kvantils pinball loss
må højst stige 5 %, og intervaldækningen må højst falde fem procentpoint mod
baseline. Ved nul baselinefejl tillades ingen stigning i det pågældende fejlmål.
Alle målinger skal være endelige tal med ovenstående datadækning. Et område,
der ikke består, fortsætter med sin eksisterende model på hele horisonten.

Leverance: rapport med perioder, dataproveniens, målinger og beslutning pr. område.
Hvis kandidaten ikke består, stop udrulningen og behold baseline. Vælg ikke en
svagere acceptregel efter at have set resultatet.

## 3. Pak og vælg de to modeller som én version, hvis forsøget består

- Udvid det eksisterende manifest med et eksplicit understøttet format til ét
  langt P10/P50/P90-sæt og et valgfrit kort P10/P50/P90-sæt samt det faste split
  ved step 192. Kopiér det valgte lange sæts uforanderlige filer ind i pakken,
  så pakken ikke afhænger af en anden registrerings oprydningslivscyklus.
- Behold én aktiv `model_versions`-række pr. område og ét `model_version`-id på
  hvert forecast-run. Manifest og `metrics_json` registrerer begge komponenters
  identitet, featurekontrakt og målinger. Ingen databaseændring er nødvendig,
  medmindre integrationen påviser et konkret behov; eksisterende felter genbruges.
- Ældre format-1-manifester og legacy-mapper forbliver læsbare som én model for
  hele horisonten. Et bundle er kun brugbart, hvis alle krævede artefakter og
  featurekontrakter valideres. Genbrug stibegrænsning og atomiske filwrites.
- Artefaktvalg, `ready_models`, `forecast_ready` og træningens kontrol af en
  brugbar aktiv model skal forstå både enkeltmodel og bundle. Bootstrap må ikke
  starte dyr genoptræning alene fordi korttidskomponenten endnu ikke findes.
- Hvis det eneste aktive bundle har en manglende eller defekt korttidsartefakt,
  fejler jobbet synligt og bevarer historikken. En tidligere, inaktiv version
  vælges ikke automatisk: operatøren følger det eksplicitte rollback i afsnit 6.
  En pinned udstedelse må ikke lydløst skifte version eller blande komponenter.
- Genbrug den nuværende worker-lås. Færdiggør og validér alle filer, før den
  aktive registrering skiftes atomisk i én transaktion. Afviste kandidater og
  afbrudt træning må ikke deaktivere den fungerende version.

## 4. Inference, drift og kompatibilitet

- Byg input én gang pr. område og udstedelse. Baseline giver hele horisonten;
  for et godkendt bundle overtager korttidskomponenten steps 1–192, mens den
  lange leverer 193–768. Hver komponents kolonner justeres til dens eget manifest.
  Step 192/193 er defineret på det eksisterende kvartersgrid, også ved off-grid
  udstedelse; indsæt ikke et ekstra punkt eller et gennemsnit ved grænsen.
- Bevar sortering af P10/P50/P90 pr. punkt og officielle spotprisers forrang i
  visningen. Intet skift af enheder, moms eller tariffer. Den lokale efterfølgende
  priskorrektion skal være frakoblet i både forsøget og denne produktionsvej.
- Mål spring ved 48-timersgrænsen i forsøget og ved fremadrettet afprøvning.
  Hvis splicingen giver uforklarede spring, afvis udrulning og undersøg dem;
  tilføj ikke automatisk udglatning, som ikke er blevet evalueret.
- Bevar eksisterende API-felter, pagination, historik og 24/72/168-målinger.
  Rapportér de nye ukendt-pris-målinger særskilt med tydelige navne i
  `metrics_json`/forsøgsrapport; omdefinér ikke gamle accuracytal lydløst.
  `model_version` refererer til hele bundlet. Feature-importance-endpointet
  beholder sin listekontrakt og returnerer den lange komponents værdier som hidtil;
  kortkomponentens diagnostik gemmes særskilt i manifest/metrics, ikke blandet ind.
- Udvid retræningsjobbet til sekventiel træning/evaluering af komponenterne og
  én samlet registrering efter accept. Del fælles datahentning mellem områder
  som i dag. Ved fejl eller afvist kortkandidat beholdes den tidligere aktive
  pakke; long-only er fortsat den eksisterende førstegangsbaseline.
- Mål køretid, peak-RAM og almindelige ingestionjobs' ventetid på NAS'en før
  udrulning. Seks estimatorer pr. område må ikke antages at passe i de 1536 MiB
  fra reference-composefilen; verificér også NAS'ens faktiske ressourcegrænser.
  Overskrides det aftalte budget, stop udrulningen og mål en mindre
  kandidat; ingen parallel tuning eller ekstra worker som automatisk løsning.

## 5. Berørte steder og checks ved implementering

| Ansvar | Eksisterende steder, der skal gennemgås |
|---|---|
| Horisonter, sampling og as-of | `backend/src/nordpool_predictor/ml/{horizon,features,asof}.py` |
| Træning, tuning og promotion | `backend/src/nordpool_predictor/ml/train.py` |
| Bundle, loading og diagnostik | `backend/src/nordpool_predictor/ml/artifacts.py`, `api/routers/models.py` |
| Routing, persistens og fallback | `backend/src/nordpool_predictor/ml/predict.py` |
| Bootstrap, retrain, cancellation | `backend/src/nordpool_predictor/scheduler/{bootstrap,jobs}.py`, `worker.py` |
| Scoring og API-kompatibilitet | `backend/src/nordpool_predictor/ml/score.py`, `api/routers/{forecasts,health,homeassistant,tariffs}.py` |
| Historik og klienter | API-skemaer, `web/lib/api.ts`, `web/lib/combined-price-data.ts`, modelinfovisning |
| Eksperiment og drift | Et fokuseret benchmark under `backend/scripts/`, README og `docs/nas-recovery.md` |

Genbrug eksisterende fixtures og tests for as-of, horisonter, tuning, scoring,
artefakter, bootstrap, modelvalg, historik og worker. Tilføj fokuserede checks for:

1. Træningens korte horisonter, fælles tidsgrænser og ingen fremtidige labels eller
   inputs. Ændring af et target, der var ukendt ved udstedelse, må ikke ændre input.
2. Eksklusion af publicerede targets, begge døgnintervaller, parrede samples,
   utilstrækkelige data, nulfejl og uændret stopbeslutning ved afvisning.
3. Format-1/legacy og nyt bundle; manglende/defekte filer; forkert område/featurekontrakt;
   atomisk registrering, rollback og bevarelse af den aktive version ved cancellation.
   Et defekt eneste aktivt bundle med en brugbar inaktiv forgænger skal give synlig
   fejl; først eksplicit reaktivering må gøre forgængeren valgbar igen.
4. Routing ved 191/192/193/768, off-grid udstedelse, sommertid/vintertid, sammenhængende
   kvarterer, ingen dubletter og P10 ≤ P50 ≤ P90. Long-only/fallback dækker hele grid'et.
5. Ét samlet versions-id i persistens, historik, latest, readiness og diagnostik;
   uændret API-kontrakt, kendte spotpriser og gammelt scoringresultat.

Kør backendens Ruff-check, format-check og hele pytest-suiten med
`TEST_DATABASE_URL` til en isoleret PostgreSQL 18-testdatabase. Skippede
PostgreSQL-tests er ikke en godkendelse. Kør webens eksisterende typechecks og
relevante kontrakt-/browserregressioner, hvis klientkode ændres. Gem resultaterne.

## 6. Udrulning og rollback

1. Afslut kodereview, tests, datagates og NAS-måling før promotion. En accepteret
   kandidat kan trænes endeligt på tilgængelig historik efter den afsluttende test;
   testmålinger må ikke ommærkes til målinger af dette refit.
2. Bevar den tidligere aktive versions filer og registrering. Afprøv først bundle
   på kopi af database/modelmappe; tag backup før ændringer på NAS'en.
3. Opdatér API og worker til samme verificerede image-digest i den faktiske
   `home-setup/compose.yaml`. Web opdateres kun ved klientændringer. Én worker
   og de eksisterende database- og modelvolumener bevares.
4. Generér et nyt komplet run, kontrollér begge områder, valgt komponent pr.
   horisont, logs, readiness, syv synlige dage og buffer. Observer faktiske
   0–24/24–48-fejl og intervaldækning, efterhånden som priser bliver tilgængelige.
5. Ved fejl: stop nye modeljobs, aktivér den bevarede long-only-version og
   generér et nyt long-only-run. Et rollback af modelregistreringen alene ændrer
   ikke allerede gemte latest-runs. Ved kodefejl rul API og worker samlet tilbage
   til det tidligere image; bevar prognosehistorik og snapshots.

## Definition af færdig

Planopgaven er færdig, når denne fil findes og en særskilt reviewer med de
anmodede skills har gennemgået den endelige version uden resterende findings.

En senere implementering er først færdig, når forsøget kan gentages, hvert
promoveret område består de fastlåste datagates, den samlede versionskontrakt og
fallback er testet med PostgreSQL, NAS-budgettet er målt, og udrulning/rollback er
verificeret. Hvis forsøget afviser kandidaten, er resultatet en dokumenteret
afvisning med eksisterende produktion bevaret, ikke en påstået forbedring.
