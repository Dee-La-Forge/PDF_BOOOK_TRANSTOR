# Frogger

Traduction en français d'ouvrages techniques au format PDF, **en conservant la
mise en page d'origine** : le texte anglais est retiré du PDF et le français
réinséré à l'emplacement exact qu'il occupait.

Conçu pour la littérature quantitative : le code, les formules, les tableaux et
les figures ne passent jamais par le traducteur.

Ouvrage de référence du POC : *Advances in Financial Machine Learning*,
Marcos López de Prado (Wiley, 2018) — 393 pages.

---

## Principe

Le pipeline repose sur une observation simple : dans un ouvrage composé
professionnellement, **la police dit la nature du contenu**. Dans l'ouvrage de
référence :

| Police | Volume | Nature | Traitement |
|---|---|---|---|
| `TimesLTStd` Roman / Italic / Bold | ~630 k car. | corps de texte | **traduit** |
| `CourierStd` | ~60 k car. | extraits Python | intact |
| `STIXMath*`, `CMSY10`, `SymbolStd` | ~12 k car. | formules | intact |
| `HelveticaLTStd` | ~9 k car. | titres de snippets, libellés | au cas par cas |

Aucune heuristique fragile n'est donc nécessaire pour décider ce qui part au
traducteur. Les fragments non traduisibles rencontrés **au fil de la prose** —
identifiants de code, variables mathématiques, indices, exposants — sont
masqués par des marqueurs `⟦n⟧`, soustraits au modèle, puis réinjectés au
rendu avec leur style d'origine.

## Les cinq étapes

| Étape | Entrée → sortie |
|---|---|
| `extract` | PDF → blocs positionnés (bbox, style, polices) en base SQLite |
| `classify` | typage : prose / titre / légende / code / formule / tableau / folio |
| `translate` | prose → français, sous contrainte de longueur, avec glossaire |
| `render` | rédaction du texte source + réinsertion du français |
| `report` | état d'avancement et blocs à relire |

L'état vit dans un répertoire de travail (`data/work` par défaut) : blocs
extraits, cache de traduction, polices, rapports. **Toute traduction déjà
payée est mise en cache** — une interruption ou une reprise d'étape ne
refacture rien.

## Installation

```powershell
py -m pip install -r requirements.txt
```

Les polices de substitution (Times New Roman, à défaut Liberation Serif ou
DejaVu Serif) sont copiées automatiquement depuis le système : les
sous-ensembles embarqués dans le PDF n'ont pas les glyphes accentués, puisque
l'original est en anglais.

Les clés API se posent dans l'environnement, ou dans un fichier `.env` à la
racine — exclu du dépôt, et lu automatiquement :

```
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=sk-...
```

## Moteurs de traduction

| `--engine` | Ce que c'est | Coût pour le livre entier |
|---|---|---|
| `claude` | API Anthropic, schéma de sortie contraint, cache du prompt système | 10-15 $ (Opus 5), 4-6 $ (Sonnet 5) |
| `deepseek` | API DeepSeek, compatible OpenAI | < 1 $ |
| `ollama` | Modèle local, rien ne sort de la machine | gratuit, mais lent |
| `fake` | Texte accentué 18 % plus long | gratuit |

Tous partagent le même socle : composition des lots, contrôle des marqueurs et
des budgets, reprise des blocs fautifs. Un moteur moins docile dégrade donc en
reprises supplémentaires — visibles dans le rapport — et non en sortie
corrompue silencieusement.

`--base-url` permet de viser n'importe quel service compatible OpenAI, et
`--model` de choisir le modèle servi.

## Utilisation

```powershell
# Repérer les pages à traiter
py -m frogger toc --pdf "Advances in Financial Machine Learning.pdf"

# Chaîne complète sur un chapitre
py -m frogger run --pdf "…\livre.pdf" --pages 130-140 --out out/ch7.pdf --subset

# Ou étape par étape
py -m frogger extract   --pdf "…\livre.pdf" --pages 130-140
py -m frogger translate --engine claude --model claude-opus-5 --effort medium
py -m frogger render    --out out/ch7.pdf --subset
py -m frogger report
```

### Traduction hors pipeline

`export` et `import-fr` ouvrent la chaîne à n'importe quel traducteur — un
autre outil, une session Claude Code, ou un relecteur humain :

```powershell
py -m frogger export    --out data/work/a_traduire.json
#  … le fichier est traduit par le moyen de votre choix, champ « fr » …
py -m frogger import-fr --in  data/work/traduit.json
```

C'est la voie utilisée pour la traduction de référence du chapitre 7 : aucun
appel API, et le résultat entre dans le cache comme n'importe quel autre.

### Mise au point sans dépense

`--engine fake` produit un texte accentué **18 % plus long** que la source.
C'est un test de charge honnête du rendu — couverture des accents, gestion des
débordements, réduction d'échelle — sans clé API et sans coût :

```powershell
py -m frogger translate --engine fake
py -m frogger render --out out/essai.pdf --subset
```

### Glossaire

`glossary.json` à la racine impose la terminologie quant (`purging` → *purge*,
`meta-labeling` → *méta-étiquetage*, `feature` → *variable explicative*…) et
liste ce qui ne doit jamais être traduit (bibliothèques, sigles, noms propres).

Il est fait pour être édité. Son empreinte entre dans la clé de cache : changer
un terme ne fait retraduire que les blocs qui le contiennent.

## Gestion du débordement

Le français occupe 15 à 20 % de place en plus que l'anglais. Quatre leviers,
appliqués dans cet ordre :

1. **Budget de caractères imposé au traducteur** — chaque bloc annonce au
   modèle la place dont il dispose ; il resserre sa formulation plutôt que de
   faire déborder la page. Un bloc rendu trop long est renvoyé au modèle.
2. **Remise à niveau de la boîte** — les boîtes que PyMuPDF renvoie épousent
   les glyphes, sans les jambages ni l'interligne complet ; elles sont
   ramenées à la hauteur réellement nécessaire.
3. **Exploitation de la gouttière** — le bloc s'étend vers le bas, puis vers le
   haut, jusqu'aux voisins immédiats.
4. **Réduction d'échelle** — jusqu'à 88 % du corps d'origine (`--min-scale`).

Si tout cela ne suffit pas, le texte est inséré à l'échelle nécessaire, aussi
petite soit-elle, et le bloc est signalé dans `reports/rendu.csv`. **Aucun
texte n'est jamais tronqué ni perdu silencieusement.**

## Résultats sur le chapitre 7

**Traduction réelle**, pages 130-136 (`out/ch7_fr.pdf`) :

| | |
|---|---|
| Blocs traduits | 50 |
| Rendus au corps d'origine | 45 |
| Compressés (95-87 %) | 5 |
| **Non insérés** | **0** |

**Test de charge** au moteur factice (+18 % imposés à *tous* les blocs, y
compris les titres courts), pages 130-140 : 78 blocs traduits, 45 au corps
d'origine, 28 compressés, 5 tassés, 0 non inséré. Les cinq blocs tassés sont
tous sur la page de bibliographie, où les entrées sont serrées sans gouttière
exploitable.

L'écart entre les deux mesures tient au budget de caractères : une vraie
traduction française le respecte, le moteur factice le viole par construction.

## Limites connues

- **Retrait négatif des listes perdu** — puces et numéros sont réinsérés au fil
  du paragraphe, sans l'alinéa suspendu d'origine.
- **Titre courant et folio fusionnés** — quand ils forment un seul bloc, le
  grand blanc qui les séparait devient une espace simple.
- **Italique d'emphase perdue** — l'italique n'est conservée que sur un bloc
  entier ou sur une variable mathématique isolée ; une locution en italique au
  milieu d'un paragraphe ressort en romain.
- **Paragraphes coupés par la segmentation** — PyMuPDF scinde parfois une liste
  imbriquée au milieu d'une phrase. La traduction se fait page par page, ce qui
  donne au modèle le contexte des blocs voisins, mais la coupure subsiste.
- **Tableaux non traduits** — ils sont détectés et laissés intacts.
- **Dérive verticale** — chaque bloc s'étendant vers le bas, un paragraphe long
  peut venir au contact du suivant. Signalé dans le rapport.

## Structure

```
frogger/
  config.py     polices de substitution, budgets, répertoire de travail
  models.py     Block, Kind, RenderStat
  store.py      persistance SQLite + cache de traduction
  extract.py    PDF → blocs, protection des fragments, césures
  classify.py   typage par police et géométrie
  glossary.py   chargement et empreinte du glossaire
  translate.py  API Claude, lots par page, contrôle des marqueurs
  render.py     rédaction, géométrie de réinsertion, repli de glyphes
  report.py     tableaux console et CSV
  cli.py        commandes
glossary.json   terminologie quant imposée
```
