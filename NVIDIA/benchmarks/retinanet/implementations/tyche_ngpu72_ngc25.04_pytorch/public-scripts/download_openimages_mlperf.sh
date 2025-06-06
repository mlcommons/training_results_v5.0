#!/bin/bash

# Copyright (c) 2021-2025, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#           http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


: "${DATASET_PATH:=/workspace/ssd_dataset/open-images-v6}"

while [ "$1" != "" ]; do
    case $1 in
        -d | --dataset-path  )        shift
                                      DATASET_PATH=$1
                                      ;;
    esac
    shift
done

MLPERF_CLASSES=('Airplane' 'Antelope' 'Apple' 'Backpack' 'Balloon' 'Banana'
'Barrel' 'Baseball bat' 'Baseball glove' 'Bee' 'Beer' 'Bench' 'Bicycle'
'Bicycle helmet' 'Bicycle wheel' 'Billboard' 'Book' 'Bookcase' 'Boot'
'Bottle' 'Bowl' 'Bowling equipment' 'Box' 'Boy' 'Brassiere' 'Bread'
'Broccoli' 'Bronze sculpture' 'Bull' 'Bus' 'Bust' 'Butterfly' 'Cabinetry'
'Cake' 'Camel' 'Camera' 'Candle' 'Candy' 'Cannon' 'Canoe' 'Carrot' 'Cart'
'Castle' 'Cat' 'Cattle' 'Cello' 'Chair' 'Cheese' 'Chest of drawers' 'Chicken'
'Christmas tree' 'Coat' 'Cocktail' 'Coffee' 'Coffee cup' 'Coffee table' 'Coin'
'Common sunflower' 'Computer keyboard' 'Computer monitor' 'Convenience store'
'Cookie' 'Countertop' 'Cowboy hat' 'Crab' 'Crocodile' 'Cucumber' 'Cupboard'
'Curtain' 'Deer' 'Desk' 'Dinosaur' 'Dog' 'Doll' 'Dolphin' 'Door' 'Dragonfly'
'Drawer' 'Dress' 'Drum' 'Duck' 'Eagle' 'Earrings' 'Egg (Food)' 'Elephant'
'Falcon' 'Fedora' 'Flag' 'Flowerpot' 'Football' 'Football helmet' 'Fork'
'Fountain' 'French fries' 'French horn' 'Frog' 'Giraffe' 'Girl' 'Glasses'
'Goat' 'Goggles' 'Goldfish' 'Gondola' 'Goose' 'Grape' 'Grapefruit' 'Guitar'
'Hamburger' 'Handbag' 'Harbor seal' 'Headphones' 'Helicopter' 'High heels'
'Hiking equipment' 'Horse' 'House' 'Houseplant' 'Human arm' 'Human beard'
'Human body' 'Human ear' 'Human eye' 'Human face' 'Human foot' 'Human hair'
'Human hand' 'Human head' 'Human leg' 'Human mouth' 'Human nose' 'Ice cream'
'Jacket' 'Jeans' 'Jellyfish' 'Juice' 'Kitchen & dining room table' 'Kite'
'Lamp' 'Lantern' 'Laptop' 'Lavender (Plant)' 'Lemon' 'Light bulb' 'Lighthouse'
'Lily' 'Lion' 'Lipstick' 'Lizard' 'Man' 'Maple' 'Microphone' 'Mirror'
'Mixing bowl' 'Mobile phone' 'Monkey' 'Motorcycle' 'Muffin' 'Mug' 'Mule'
'Mushroom' 'Musical keyboard' 'Necklace' 'Nightstand' 'Office building'
'Orange' 'Owl' 'Oyster' 'Paddle' 'Palm tree' 'Parachute' 'Parrot' 'Pen'
'Penguin' 'Personal flotation device' 'Piano' 'Picture frame' 'Pig' 'Pillow'
'Pizza' 'Plate' 'Platter' 'Porch' 'Poster' 'Pumpkin' 'Rabbit' 'Rifle'
'Roller skates' 'Rose' 'Salad' 'Sandal' 'Saucer' 'Saxophone' 'Scarf' 'Sea lion'
'Sea turtle' 'Sheep' 'Shelf' 'Shirt' 'Shorts' 'Shrimp' 'Sink' 'Skateboard'
'Ski' 'Skull' 'Skyscraper' 'Snake' 'Sock' 'Sofa bed' 'Sparrow' 'Spider' 'Spoon'
'Sports uniform' 'Squirrel' 'Stairs' 'Stool' 'Strawberry' 'Street light'
'Studio couch' 'Suit' 'Sun hat' 'Sunglasses' 'Surfboard' 'Sushi' 'Swan'
'Swimming pool' 'Swimwear' 'Tank' 'Tap' 'Taxi' 'Tea' 'Teddy bear' 'Television'
'Tent' 'Tie' 'Tiger' 'Tin can' 'Tire' 'Toilet' 'Tomato' 'Tortoise' 'Tower'
'Traffic light' 'Train' 'Tripod' 'Truck' 'Trumpet' 'Umbrella' 'Van' 'Vase'
'Vehicle registration plate' 'Violin' 'Wall clock' 'Waste container' 'Watch'
'Whale' 'Wheel' 'Wheelchair' 'Whiteboard' 'Window' 'Wine' 'Wine glass' 'Woman'
'Zebra' 'Zucchini')

date

python3 public-scripts/fiftyone_openimages.py \
    --dataset-dir=${DATASET_PATH} \
    --output-labels="openimages-mlperf.json" \
    --classes "${MLPERF_CLASSES[@]}"

date